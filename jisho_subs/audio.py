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
