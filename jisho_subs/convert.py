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

from .audio import SEEK_ACCURATE, AudioFile, natural_key, probe, read_tags
from .metadata import BookInfo, build_tags, parse_directory

#: Fallback AAC bitrate when the source's own rate cannot be read.
DEFAULT_BITRATE = 128
#: Re-encoding outside this range is pointless in one direction or wasteful in
#: the other: the library runs from 32 to 192 kbps.
MIN_BITRATE, MAX_BITRATE = 48, 192


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
    #: How each file was produced, as {method: count}.
    methods: dict = None
    #: The book's track total, as written into the tags.
    track_total: int = 0


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


def _target_bitrate(source: AudioFile) -> int:
    """Match the source rather than impose a fixed rate.

    Only used when the lossless remux is unavailable.  A fixed 128k quadruples
    a 32 kbps mono file for no gain and throws quality away from a 192 kbps one;
    the library holds both.
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=bit_rate",
             "-of", "default=nw=1:nk=1", source.path],
            capture_output=True, text=True, check=True).stdout.strip()
        kbps = int(out) // 1000
    except (subprocess.CalledProcessError, ValueError, OSError):
        return DEFAULT_BITRATE
    if kbps <= 0:
        return DEFAULT_BITRATE
    return max(MIN_BITRATE, min(MAX_BITRATE, kbps))


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


def _convert_one(src: str, dst: str, tags: Optional[dict] = None,
                 reencode: bool = False, bitrate: int = DEFAULT_BITRATE):
    """Returns ``(error, method)`` — error is None on success.

    The MP3 stream is *copied* into the MP4 container by default, which is
    genuinely lossless: the decoded samples come back bit-identical, the file is
    smaller than the AAC one, and it takes a quarter of a second instead of
    thirteen.  All the MP4 sample index — the thing that fixes seeking — is
    written either way.

    ``-f mp4`` is not optional there.  A ``.m4b`` extension selects ffmpeg's
    *ipod* muxer, which refuses MP3 outright ("Could not find tag for codec
    mp3"), so the remux silently becomes a failed conversion without it.

    Re-encoding to AAC stays as the fallback, for a source MP4 cannot carry and
    for ``--reencode``.
    """
    tmp = dst + ".part.m4b"
    meta = _metadata_args(tags) if tags else []
    base = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", src]
    rate = f"{bitrate}k"

    # Cover art is kept where it can be; some books ship malformed artwork, so
    # each method also has an audio-only form rather than losing the file to a
    # picture.
    lossless = [
        ("copied losslessly",
         base + ["-map", "0", "-c", "copy", "-disposition:v", "attached_pic",
                 "-f", "mp4"] + meta + [tmp]),
        ("copied losslessly",
         base + ["-map", "0:a", "-c:a", "copy", "-f", "mp4"] + meta + [tmp]),
    ]
    encoded = [
        (f"re-encoded to AAC {rate}",
         base + ["-map", "0", "-c:a", "aac", "-b:a", rate,
                 "-c:v", "copy", "-disposition:v", "attached_pic"] + meta + [tmp]),
        (f"re-encoded to AAC {rate}",
         base + ["-map", "0:a", "-c:a", "aac", "-b:a", rate] + meta + [tmp]),
    ]
    attempts = encoded if reencode else lossless + encoded

    last = ""
    for method, cmd in attempts:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, dst)
            return None, method
        last = (proc.stderr or "").strip()[:200]
        if os.path.exists(tmp):
            os.unlink(tmp)
    return (last or "ffmpeg failed"), None


def numbering(tracks: Sequence[AudioFile]) -> dict:
    """Map each track's *stem* to its ``(position, total)`` in the whole book.

    Track numbers must count the book, not the batch.  Converting one leftover
    file of twenty-two used to tag it `1/1`, and a run of four tagged the last
    one `4/4` — both wrong, and wrong in a way no player can recover from.

    Keyed by stem rather than path on purpose: the same track is `01 x.mp3`
    before conversion and `01 x.m4b` after, so a path-keyed map silently misses
    every lookup the moment the extension changes.
    """
    stems = sorted({t.stem for t in tracks}, key=natural_key)
    total = len(stems)
    return {stem: (i, total) for i, stem in enumerate(stems, 1)}


def convert(files: Sequence[AudioFile], out_dir: Optional[str] = None,
            jobs: Optional[int] = None, force: bool = False,
            on_start=None, on_done=None, reencode: bool = False,
            positions: Optional[dict] = None) -> ConvertResult:
    """Convert every non-seek-accurate file into *out_dir*.

    *positions* maps a source path to its ``(track, total)`` across the whole
    book; without it the numbering falls back to this batch alone.
    """
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
    methods: dict = {}

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
        track, total = (positions or {}).get(f.stem, (index, len(todo)))
        tags, dropped = build_tags(info, read_tags(f.path), f.name, track, total)
        if dropped:
            discarded.append((f.name, dropped))
        elif tags.get("title"):
            titled.append((f.name, tags["title"]))
        err, method = _convert_one(f.path, dst, tags, reencode=reencode,
                                   bitrate=_target_bitrate(f))
        if err is None:
            methods[method] = methods.get(method, 0) + 1
            made.append(dst)
        else:
            failed.append((f.name, err))
        if on_done:
            on_done(f)

    jobs = jobs or min(16, max(1, (os.cpu_count() or 4)))
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        list(pool.map(work, enumerate(todo, 1)))

    track_total = max((t for _, t in (positions or {}).values()), default=len(todo))
    return ConvertResult(made, skipped, failed, out_dir, info, discarded,
                         titled, methods, track_total)


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


def retag(files: Sequence[AudioFile], out_dir: Optional[str] = None,
          positions: Optional[dict] = None, jobs: Optional[int] = None,
          on_start=None, on_done=None) -> ConvertResult:
    """Rewrite the tags on already-converted audio, without re-encoding it.

    Needed because tags cannot be revised retroactively and the source MP3s may
    be long gone: a book converted across several runs carries a different track
    total from each of them, and once `-d` has removed the MP3s there is nothing
    left to convert from.  This remuxes each file with `-c copy`, so the audio
    is untouched and the work takes no measurable time.
    """
    if not files:
        return ConvertResult([], [], [], out_dir or "", None, [], [], {}, 0)
    out_dir = out_dir or os.path.dirname(os.path.abspath(files[0].path))
    info = parse_directory(out_dir)

    made: List[str] = []
    failed: List[tuple] = []
    discarded: List[tuple] = []
    titled: List[tuple] = []

    def work(item):
        index, f = item
        if on_start:
            on_start(f)
        track, total = (positions or {}).get(f.stem, (index, len(files)))
        tags, dropped = build_tags(info, read_tags(f.path), f.name, track, total)
        if dropped:
            discarded.append((f.name, dropped))
        elif tags.get("title"):
            titled.append((f.name, tags["title"]))
        tmp = f.path + ".retag.m4b"
        cmd = (["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", f.path,
                "-map", "0", "-c", "copy", "-disposition:v", "attached_pic",
                "-f", "mp4"] + _metadata_args(tags) + [tmp])
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, f.path)
            made.append(f.path)
        else:
            failed.append((f.name, (proc.stderr or "").strip()[:200]))
            if os.path.exists(tmp):
                os.unlink(tmp)
        if on_done:
            on_done(f)

    jobs = jobs or min(16, max(1, (os.cpu_count() or 4)))
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        list(pool.map(work, enumerate(files, 1)))

    total = max((t for _, t in (positions or {}).values()), default=len(files))
    return ConvertResult(made, [], failed, out_dir, info, discarded,
                         titled, {"re-tagged in place": len(made)}, total)
