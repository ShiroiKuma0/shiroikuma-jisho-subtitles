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
Originals are never touched.  The M4B is written beside its MP3, under the same
basename, so one SRT serves either; the MP3 stays exactly where it was, and
deleting it is 白い熊's decision to make.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import List, Optional, Sequence

from .audio import SEEK_ACCURATE, AudioFile, probe, read_tags
from .metadata import BookInfo, build_tags, parse_directory

#: What the app's dialog recommends.
BITRATE = "128k"


@dataclass
class ConvertResult:
    made: List[str]
    skipped: List[str]
    failed: List[tuple]
    out_dir: str
    #: What the directory name said about the book.
    info: Optional[BookInfo] = None
    #: Track titles that were dropped, as (file, reason).
    discarded: List[tuple] = None
    #: Files that got a real track title, as (file, title).
    titled: List[tuple] = None


def needs_conversion(files: Sequence[AudioFile]) -> List[AudioFile]:
    return [f for f in files
            if os.path.splitext(f.path)[1].lower() not in SEEK_ACCURATE]


def target_dir(files: Sequence[AudioFile]) -> str:
    """The audio's own directory — one folder per book, as 白い熊 keeps them.

    The M4B lands beside the MP3 it came from, sharing its basename, so the
    same SRT pairs with either.  The MP3 is left in place; removing it is 白い熊's
    call, not the tool's.  Until it goes, the app's chapter list will show both
    copies of every track — the tool itself ignores the MP3 (see
    ``audio.prefer_seekable``).
    """
    return os.path.dirname(os.path.abspath(files[0].path))


def _metadata_args(tags: dict) -> List[str]:
    out = ["-map_metadata", "-1"]      # drop the originals, write repaired ones
    for key, value in tags.items():
        if not value:
            continue
        if key == "language":
            # MP4 keeps language on the stream, not on the container: a
            # format-level -metadata language=… is accepted and then silently
            # dropped, leaving the track marked "und".
            out += ["-metadata:s:a:0", f"language={value}"]
        else:
            out += ["-metadata", f"{key}={value}"]
    return out


def _convert_one(src: str, dst: str, tags: Optional[dict] = None) -> Optional[str]:
    """Returns None on success, or the error output."""
    tmp = dst + ".part.m4b"
    meta = _metadata_args(tags) if tags else []
    base = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", src]
    # The app's recommended invocation keeps the cover art as attached_pic.
    # Some books ship malformed artwork, so fall back to audio only rather
    # than lose the file over a picture.
    attempts = [
        base + ["-map", "0", "-c:a", "aac", "-b:a", BITRATE,
                "-c:v", "copy", "-disposition:v", "attached_pic"] + meta + [tmp],
        base + ["-map", "0:a", "-c:a", "aac", "-b:a", BITRATE] + meta + [tmp],
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

    # The folder name is the reliable source for book, author, year and
    # language; the per-file tags are not.
    info = parse_directory(os.path.dirname(os.path.abspath(todo[0].path)))

    made: List[str] = []
    skipped: List[str] = []
    failed: List[tuple] = []
    discarded: List[tuple] = []
    titled: List[tuple] = []

    def work(item):
        index, f = item
        dst = os.path.join(out_dir, f.stem + ".m4b")
        if on_start:
            on_start(f)
        if not force and os.path.exists(dst) and os.path.getsize(dst) > 0:
            skipped.append(dst)
            if on_done:
                on_done(f)
            return
        tags, dropped = build_tags(info, read_tags(f.path), f.name,
                                   index, len(todo))
        if dropped:
            discarded.append((f.name, dropped))
        elif tags.get("title"):
            titled.append((f.name, tags["title"]))
        err = _convert_one(f.path, dst, tags)
        if err is None:
            made.append(dst)
        else:
            failed.append((f.name, err))
        if on_done:
            on_done(f)

    jobs = jobs or min(16, max(1, (os.cpu_count() or 4)))
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        list(pool.map(work, enumerate(todo, 1)))

    return ConvertResult(made, skipped, failed, out_dir, info, discarded, titled)


def converted_files(out_dir: str) -> List[AudioFile]:
    from .audio import discover
    return discover(out_dir)


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def verify_replacement(source: AudioFile, replacement: str,
                       tolerance: float = 0.02) -> Optional[str]:
    """Confirm an M4B really stands in for its MP3 before the MP3 is deleted.

    Deleting audio is irreversible, so "the file exists" is not enough: the
    replacement has to probe as real audio of the same length.  A truncated or
    half-written conversion would otherwise take the original with it.
    """
    if not os.path.exists(replacement) or os.path.getsize(replacement) == 0:
        return "the converted file is missing or empty"
    info = probe(replacement)
    if info is None:
        return "the converted file does not probe as audio"
    if source.duration > 0:
        drift = abs(info.duration - source.duration)
        if drift > max(1.0, source.duration * tolerance):
            return (f"length differs by {drift:.1f}s "
                    f"({source.duration:.0f}s vs {info.duration:.0f}s)")
    return None


def delete_sources(files: Sequence[AudioFile], out_dir: str,
                   log=None) -> tuple:
    """Delete each MP3 whose M4B is verified good.  Returns (deleted, kept)."""
    deleted, kept = [], []
    for f in files:
        replacement = os.path.join(out_dir, f.stem + ".m4b")
        problem = verify_replacement(f, replacement)
        if problem:
            kept.append((f.name, problem))
            if log:
                log(f"  keeping {f.name}: {problem}")
            continue
        try:
            os.unlink(f.path)
            deleted.append(f.name)
        except OSError as exc:
            kept.append((f.name, str(exc)))
    return deleted, kept
