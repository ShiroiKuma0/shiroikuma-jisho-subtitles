"""SRT output, written to shiroikuma-jisho's actual parser contract.

The app is stricter than SubRip in general, and — more importantly — its
``flattenSubtitles()`` pass *silently rewrites* what it loads.  A file that
merely "looks like valid SRT" can lose cues on the way in.  Everything enforced
here comes from reading that code:

* the timestamp regex demands ``HH:MM:SS,mmm`` with two-digit fields and a
  comma; nothing else parses;
* nothing strips a byte-order mark, so a BOM lands inside cue #1's text;
* empty cues are dropped;
* cues sharing a start **and** end are merged into one;
* adjacent cues with identical text less than 500 ms apart are merged — ten
  passes of it — which is a real hazard in dialogue («Ja.» «Ja.»);
* the final cue is dropped when ``last.end < secondLast.start``;
* HTML tags, ``{…}`` overrides, entities and emoji are stripped from the text.

Where a rule cannot be satisfied without losing text, the writer does the merge
itself so that the file on disk and what 白い熊 sees in the app agree, and the
report says it happened.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from .align import Cue

#: Below this the app merges identical adjacent cues.
IDENTICAL_MERGE_GAP = 0.500
MIN_CUE = 0.30

_TAGLIKE = re.compile(r"<[^>]{0,40}>")
_BRACELIKE = re.compile(r"\{[^}]{0,40}\}")
_WS = re.compile(r"\s+")
_TIMESTAMP = re.compile(
    r"^(\d{2}):([0-5]\d):([0-5]\d),(\d{3}) --> (\d{2}):([0-5]\d):([0-5]\d),(\d{3})$")


@dataclass
class WriteStats:
    cues: int = 0
    merged_identical: int = 0
    neutralised_markup: int = 0
    files: int = 0
    empty_files: List[str] = field(default_factory=list)


def format_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def sanitize(text: str) -> tuple[str, bool]:
    """Make text survive the app's stripping pass unchanged.

    Angle brackets and braces are swapped for their typographic lookalikes only
    when they form something the app would delete — so ordinary prose is never
    touched, but a stray ``<...>`` cannot silently eat a clause.
    """
    changed = False
    if _TAGLIKE.search(text):
        text = text.replace("<", "‹").replace(">", "›")
        changed = True
    if _BRACELIKE.search(text):
        text = text.replace("{", "❴").replace("}", "❵")
        changed = True
    return _WS.sub(" ", text).strip(), changed


def _prepare(cues: Sequence[Cue], duration: float, stats: WriteStats) -> List[Cue]:
    """Apply the contract to one file's cues."""
    ordered = sorted((c for c in cues), key=lambda c: (c.start, c.end))
    out: List[Cue] = []
    for cue in ordered:
        text, changed = sanitize(cue.sentence.text)
        if changed:
            stats.neutralised_markup += 1
        if not text:
            continue
        cue = Cue(cue.sentence, cue.file_index, cue.start, cue.end,
                  cue.confidence, cue.interpolated)
        cue.sentence = type(cue.sentence)(text, cue.sentence.doc,
                                          cue.sentence.is_heading,
                                          cue.sentence.block_index)
        # Identical text too close together would be merged by the app on load;
        # do it here so the file and the app agree.
        if out and out[-1].sentence.text == text \
                and cue.start - out[-1].end < IDENTICAL_MERGE_GAP:
            out[-1].end = max(out[-1].end, cue.end)
            stats.merged_identical += 1
            continue
        out.append(cue)

    # Strictly increasing, non-overlapping, inside the file, ordered tail.
    prev_end = 0.0
    cleaned: List[Cue] = []
    for cue in out:
        cue.start = max(cue.start, prev_end)
        cue.end = max(cue.end, cue.start + MIN_CUE)
        if duration > 0:
            cue.end = min(cue.end, duration)
            cue.start = min(cue.start, max(0.0, cue.end - 0.01))
        if cue.end <= cue.start:
            continue
        prev_end = cue.end
        cleaned.append(cue)
    return cleaned


def render(cues: Sequence[Cue]) -> str:
    parts: List[str] = []
    for i, cue in enumerate(cues, 1):
        parts.append(str(i))
        parts.append(f"{format_timestamp(cue.start)} --> {format_timestamp(cue.end)}")
        parts.append(cue.sentence.text)
        parts.append("")
    return "\n".join(parts)


def write_for_files(cues: Sequence[Optional[Cue]], files, out_dir: Optional[str],
                    stats: Optional[WriteStats] = None, log=None,
                    on_file=None) -> WriteStats:
    """Write one SRT per audio file, named after the audio file.

    The app pairs audio with subtitles by identical basename in the same
    directory (`_findCompanionSrt()`), so this naming is what makes the SRT
    attach itself with no further action.
    """
    stats = stats or WriteStats()
    log = log or (lambda *a, **k: None)

    by_file: dict[int, List[Cue]] = {}
    for cue in cues:
        if cue is not None:
            by_file.setdefault(cue.file_index, []).append(cue)

    for fi, audio in enumerate(files):
        target_dir = out_dir or os.path.dirname(audio.path)
        os.makedirs(target_dir, exist_ok=True)
        path = os.path.join(target_dir, audio.stem + ".srt")
        prepared = _prepare(by_file.get(fi, []), audio.duration, stats)
        if not prepared:
            # Wholly unmatched audio is normal — a publisher intro track, for
            # instance — so say so rather than failing.
            stats.empty_files.append(audio.name)
            log(f"  no text  {audio.name}")
            if on_file is not None:
                on_file(audio.name)
            continue
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(render(prepared))
        stats.cues += len(prepared)
        stats.files += 1
        log(f"  wrote    {os.path.basename(path)}  ({len(prepared)} cues)")
        if on_file is not None:
            on_file(audio.name)
    return stats


# -- verification --------------------------------------------------------

def lint(path: str) -> List[str]:
    """Check a written SRT against every rule the app imposes."""
    problems: List[str] = []
    raw = open(path, "rb").read()
    if raw.startswith(b"\xef\xbb\xbf"):
        problems.append("file starts with a UTF-8 BOM")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return problems + [f"not valid UTF-8: {exc}"]

    blocks = [b for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]
    prev_end = None
    prev_text = None
    spans: List[tuple[float, float]] = []
    for n, block in enumerate(blocks, 1):
        lines = block.split("\n")
        if len(lines) < 3:
            problems.append(f"cue {n}: fewer than three lines")
            continue
        if lines[0].strip() != str(n):
            problems.append(f"cue {n}: index is {lines[0].strip()!r}")
        m = _TIMESTAMP.match(lines[1].strip())
        if not m:
            problems.append(f"cue {n}: bad timestamp line {lines[1]!r}")
            continue
        g = [int(x) for x in m.groups()]
        start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        body = "\n".join(lines[2:]).strip()
        if not body:
            problems.append(f"cue {n}: empty text (the app would drop it)")
        if _TAGLIKE.search(body) or _BRACELIKE.search(body):
            problems.append(f"cue {n}: markup the app would strip")
        if end <= start:
            problems.append(f"cue {n}: end is not after start")
        if prev_end is not None:
            if start < prev_end:
                problems.append(f"cue {n}: overlaps the previous cue")
            if body == prev_text and start - prev_end < IDENTICAL_MERGE_GAP:
                problems.append(f"cue {n}: identical to the previous cue and "
                                f"{(start - prev_end) * 1000:.0f} ms after it "
                                f"(the app would merge them)")
        spans.append((start, end))
        prev_end, prev_text = end, body

    if len(spans) >= 2 and spans[-1][1] < spans[-2][0]:
        problems.append("final cue would be dropped (last.end < secondLast.start)")
    if len({s for s in spans}) != len(spans):
        problems.append("two cues share identical start and end (the app merges them)")
    return problems
