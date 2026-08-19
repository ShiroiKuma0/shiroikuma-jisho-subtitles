"""Convert MP3 audiobooks to M4B, because the app cannot seek MP3.

shiroikuma-jisho shows this when an MP3 is attached:

    The Android audio player cannot seek MP3 files accurately — Prev/Next and
    the slider will land several seconds away from the requested position, and
    **auto-pause will fire at the wrong sentence boundaries**.  This is a
    limitation of the MP3 format and the player; not something the app can fix.

That last part is what makes this part of the pipeline rather than an optional
extra.  The entire study workflow is "play one sentence, stop" — if auto-pause
fires at the wrong boundary, precisely-aligned subtitles are wasted.  MP4 keeps
a sample-accurate index, so seeks land where they are asked to.

The command is the app's own recommendation, verbatim, so the two stay in step.
Originals are never touched: the M4Bs are written to a sibling directory and the
MP3s stay exactly where they were.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import List, Optional, Sequence

from .audio import AudioFile, probe

#: Containers that already carry a sample-accurate index.
SEEK_ACCURATE = {".m4a", ".m4b", ".mp4", ".aac", ".ogg", ".opus", ".flac", ".wav"}

#: What the app's dialog recommends.
BITRATE = "128k"

SUFFIX = " [m4b]"


@dataclass
class ConvertResult:
    made: List[str]
    skipped: List[str]
    failed: List[tuple]
    out_dir: str


def needs_conversion(files: Sequence[AudioFile]) -> List[AudioFile]:
    return [f for f in files
            if os.path.splitext(f.path)[1].lower() not in SEEK_ACCURATE]


def target_dir(files: Sequence[AudioFile]) -> str:
    """A sibling directory beside the audio, so the originals stay put."""
    src = os.path.dirname(os.path.abspath(files[0].path))
    return src + SUFFIX


def _convert_one(src: str, dst: str) -> Optional[str]:
    """Returns None on success, or the error output."""
    tmp = dst + ".part.m4b"
    base = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", src]
    # The app's recommended invocation keeps the cover art as attached_pic.
    # Some books ship malformed artwork, so fall back to audio only rather
    # than lose the file over a picture.
    attempts = [
        base + ["-map", "0", "-c:a", "aac", "-b:a", BITRATE,
                "-c:v", "copy", "-disposition:v", "attached_pic", tmp],
        base + ["-map", "0:a", "-c:a", "aac", "-b:a", BITRATE, tmp],
    ]
    last = ""
    for cmd in attempts:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, dst)
            return None
        last = (proc.stderr or "").strip()[:200]
        if os.path.exists(tmp):
            os.unlink(tmp)
    return last or "ffmpeg failed"


def convert(files: Sequence[AudioFile], out_dir: Optional[str] = None,
            jobs: Optional[int] = None, force: bool = False,
            on_start=None, on_done=None) -> ConvertResult:
    """Convert every non-seek-accurate file into *out_dir*."""
    todo = needs_conversion(files)
    if not todo:
        return ConvertResult([], [], [], out_dir or "")
    out_dir = out_dir or target_dir(todo)
    os.makedirs(out_dir, exist_ok=True)

    made: List[str] = []
    skipped: List[str] = []
    failed: List[tuple] = []

    def work(f: AudioFile):
        dst = os.path.join(out_dir, f.stem + ".m4b")
        if on_start:
            on_start(f)
        if not force and os.path.exists(dst) and os.path.getsize(dst) > 0:
            skipped.append(dst)
            if on_done:
                on_done(f)
            return
        err = _convert_one(f.path, dst)
        if err is None:
            made.append(dst)
        else:
            failed.append((f.name, err))
        if on_done:
            on_done(f)

    jobs = jobs or min(16, max(1, (os.cpu_count() or 4)))
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        list(pool.map(work, todo))

    return ConvertResult(made, skipped, failed, out_dir)


def converted_files(out_dir: str) -> List[AudioFile]:
    from .audio import discover
    return discover(out_dir)


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None
