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


def discover(directory: str) -> List[AudioFile]:
    """Find every audio file under *directory* in natural order.

    Accepts either the directory holding the audio, or a book directory with the
    audio in a single subdirectory.
    """
    candidates: List[str] = []
    for entry in sorted(os.listdir(directory), key=natural_key):
        full = os.path.join(directory, entry)
        if os.path.isfile(full) and os.path.splitext(entry)[1].lower() in AUDIO_EXTS:
            candidates.append(full)

    if not candidates:
        subdirs = [os.path.join(directory, d) for d in sorted(os.listdir(directory))
                   if os.path.isdir(os.path.join(directory, d))]
        for sub in subdirs:
            found = discover(sub)
            if found:
                candidates.extend(f.path for f in found)

    files = []
    for path in candidates:
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
