"""Audio discovery and probing.

Files are ordered by *natural sort*, the same comparison shiroikuma-jisho uses
in ``_listChapterAudios()``, so the tool's idea of chapter order and the app's
always agree.

There is deliberately no global timeline here.  Because the output is one SRT
per audio file, each file is transcribed on its own and keeps its own local
timestamps; the alignment only needs the files concatenated in order, not
sample-exact cumulative offsets.  That designs out MP3 encoder-delay drift and
``ffprobe``'s bitrate-estimated durations rather than trying to correct them.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from typing import List, Optional

#: What the app itself will open, plus opus.
AUDIO_EXTS = {".mp3", ".m4a", ".m4b", ".ogg", ".opus", ".wav", ".flac", ".aac"}

#: Containers carrying a sample-accurate index.  MP3 does not: the Android
#: player estimates the seek target from average bitrate, lands seconds away,
#: and auto-pause then fires at the wrong sentence.
SEEK_ACCURATE = {".m4a", ".m4b", ".mp4", ".aac", ".ogg", ".opus", ".flac", ".wav"}

_NUM = re.compile(r"(\d+)")


@dataclass
class AudioFile:
    path: str
    duration: float
    codec: str
    container: str

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def stem(self) -> str:
        return os.path.splitext(self.name)[0]


def natural_key(name: str):
    """Split into text/number runs so ``2 Foo`` sorts before ``10 Foo``."""
    return [int(p) if p.isdigit() else p.lower() for p in _NUM.split(name)]


def probe(path: str) -> Optional[AudioFile]:
    """Read duration and codec from the container itself.

    The extension is never trusted: one of the validation books ships genuine
    MP3 data in files carrying MP4 ``major_brand=isom`` metadata, and another
    uses ``.m4b`` for what is simply AAC audio.
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name", "-show_entries",
             "format=duration,format_name", "-of", "json", path],
            capture_output=True, text=True, check=True).stdout
        info = json.loads(out)
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError):
        return None

    streams = info.get("streams") or []
    fmt = info.get("format") or {}
    if not streams:
        return None
    try:
        duration = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    return AudioFile(path, duration,
                     streams[0].get("codec_name", "?"),
                     fmt.get("format_name", "?"))


def _audio_in(directory: str) -> List[str]:
    out = []
    for entry in sorted(os.listdir(directory), key=natural_key):
        full = os.path.join(directory, entry)
        if os.path.isfile(full) and os.path.splitext(entry)[1].lower() in AUDIO_EXTS:
            out.append(full)
    return out


def _seekable_share(paths: List[str]) -> float:
    """Fraction of a set that can be seeked accurately."""
    if not paths:
        return 0.0
    good = sum(1 for p in paths
               if os.path.splitext(p)[1].lower() in SEEK_ACCURATE)
    return good / len(paths)


def prefer_seekable(paths: List[str], on_shadow=None) -> List[str]:
    """Where a track exists twice, keep the seek-accurate copy.

    Converted books hold `001 Track.mp3` and `001 Track.m4b` side by side in one
    folder.  Both are the same audio, so processing both would double every
    chapter; the M4B is the one the app can seek, so it wins and the MP3 is
    shadowed.
    """
    by_stem: dict = {}
    for path in paths:
        stem = os.path.splitext(os.path.basename(path))[0]
        by_stem.setdefault(stem, []).append(path)

    kept, shadowed = [], []
    for stem in sorted(by_stem, key=natural_key):
        group = by_stem[stem]
        if len(group) == 1:
            kept.append(group[0])
            continue
        good = [p for p in group
                if os.path.splitext(p)[1].lower() in SEEK_ACCURATE]
        winner = good[0] if good else group[0]
        kept.append(winner)
        shadowed.extend(p for p in group if p != winner)
    if shadowed and on_shadow:
        on_shadow(shadowed)
    return kept


def discover(directory: str, on_choice=None, on_shadow=None) -> List[AudioFile]:
    """Find a book's audio, in natural order.

    Accepts either the directory holding the audio, or a book directory with
    the audio in a subdirectory.  When several subdirectories hold audio — which
    is what converting to M4B produces — exactly one is chosen rather than the
    two being concatenated, preferring the set the app can seek accurately.
    """
    paths = _audio_in(directory)

    if not paths:
        groups = []
        for name in sorted(os.listdir(directory), key=natural_key):
            sub = os.path.join(directory, name)
            if not os.path.isdir(sub):
                continue
            found = _audio_in(sub)
            if not found:
                nested = discover(sub)
                found = [f.path for f in nested]
            if found:
                groups.append((sub, found))
        if not groups:
            return []
        if len(groups) > 1:
            groups.sort(key=lambda g: (_seekable_share(g[1]), len(g[1])),
                        reverse=True)
            if on_choice:
                on_choice(groups[0][0], [g[0] for g in groups[1:]])
        paths = groups[0][1]

    paths = prefer_seekable(paths, on_shadow)
    files = []
    for path in paths:
        info = probe(path)
        if info is not None:
            files.append(info)
    files.sort(key=lambda f: natural_key(f.path))
    return files


def total_duration(files: List[AudioFile]) -> float:
    return sum(f.duration for f in files)


def format_hms(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}"


def decode(path: str, sampling_rate: int = 16000):
    """Decode to mono float32 through ffmpeg rather than PyAV.

    faster-whisper decodes with PyAV by default, and PyAV gives up early on
    files carrying malformed embedded artwork.  One validation book ships a
    JPEG cover labelled as PNG; PyAV returned **106.97 s of a 2071.75 s file**
    — five per cent of the audio — and raised nothing.  The pipeline then
    produced a full set of confident-looking SRTs covering a twentieth of the
    book.  Silent loss that still yields plausible output is the worst failure
    mode there is, so decoding goes through ffmpeg, which reads these files
    correctly.  ``-vn -sn -dn`` drops the offending art outright.
    """
    import numpy as np

    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-threads", "0", "-i", path,
         "-vn", "-sn", "-dn",
         "-f", "f32le", "-acodec", "pcm_f32le",
         "-ac", "1", "-ar", str(sampling_rate), "-"],
        capture_output=True, check=True)
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


def decoded_seconds(samples, sampling_rate: int = 16000) -> float:
    return len(samples) / float(sampling_rate)


def check_decode(path: str, samples, expected: float, sampling_rate: int = 16000,
                 tolerance: float = 0.02, log=None) -> bool:
    """Warn when a decode came up short of the container's own duration.

    This is the guard that turns the failure above into something visible on
    the first file instead of something discovered after a full run.
    """
    got = decoded_seconds(samples, sampling_rate)
    if expected <= 0:
        return True
    if got < expected * (1.0 - tolerance):
        if log:
            log(f"  decoded only {got:.0f}s of {expected:.0f}s from "
                f"{os.path.basename(path)} — the file may be damaged")
        return False
    return True
