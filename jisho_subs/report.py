"""The run report.

Silent truncation is the failure mode that actually costs study time: a book
that quietly loses forty sentences still produces a directory full of
plausible-looking SRTs.  So everything the pipeline discarded is named here —
which reference documents were dropped, which audio no sentence claimed, which
cues were interpolated rather than matched.
"""

from __future__ import annotations

import json
import os
from typing import List, Optional, Sequence

from .align import AlignStats, Cue
from .audio import format_hms
from .srt import WriteStats


#: Faster than any narrator manages — chars/s for character languages, words/s
#: otherwise.  A cue allotted less time than this cannot be what was spoken.
_CEILING = {"ja": 14.0, "zh": 14.0}
_CEILING_DEFAULT = 5.0


def implausible(cues, lang: str, margin: float = 0.7):
    """Interpolated cues too short for their own text to have been read.

    These arise when two matched neighbours sit almost adjacent in time but
    have unmatched sentences between them: the proportional share collapses to
    the minimum cue length.  The text is real, so it is not dropped — but the
    timing is wrong, and the app will flash it for a third of a second and stop.
    Worth naming rather than shipping quietly.
    """
    ceiling = _CEILING.get(lang, _CEILING_DEFAULT)
    out = []
    for c in cues:
        if c is None or not c.interpolated:
            continue
        size = (len(c.sentence.text) if lang in _CEILING
                else len(c.sentence.text.split()))
        needed = size / ceiling
        got = c.end - c.start
        if got < needed * margin:
            out.append((c, got, needed))
    out.sort(key=lambda t: t[1] / max(t[2], 1e-6))
    return out


def _pct(part: int, whole: int) -> str:
    return f"{100.0 * part / whole:.1f}%" if whole else "n/a"


def build(book: str, lang: str, source: str, files, sentences,
          cues: Sequence[Optional[Cue]], stats: AlignStats,
          write_stats: WriteStats, dropped_docs: Sequence[tuple],
          low_confidence: int = 20) -> str:
    total_audio = sum(f.duration for f in files)
    placed = [c for c in cues if c is not None]
    matched = [c for c in placed if not c.interpolated]
    lines: List[str] = []
    add = lines.append

    add("=" * 72)
    add(f"  {book}")
    add("=" * 72)
    add(f"  language          {lang}")
    add(f"  reference         {os.path.basename(source)}")
    add(f"  audio             {len(files)} files, {format_hms(total_audio)}")
    add("")
    add("  TEXT")
    add(f"    sentences       {len(sentences)}")
    add(f"    placed          {len(matched)}  ({_pct(len(matched), len(sentences))})")
    add(f"    interpolated    {stats.interpolated}"
        f"   [timed by proportion, not matched]")
    add(f"    dropped, front  {stats.dropped_leading}"
        f"   [before the first spoken sentence]")
    add(f"    dropped, back   {stats.dropped_trailing}"
        f"   [after the last spoken sentence]")
    add(f"    dropped, inside {stats.dropped_interior}"
        f"   [gaps too wide or across a file boundary]")

    if dropped_docs:
        add("")
        add("  REFERENCE DOCUMENTS DROPPED AS DUPLICATES (contents pages)")
        for doc, blocks, ratio in dropped_docs:
            add(f"    {doc}   {blocks} blocks, {ratio:.0%} found elsewhere")

    if stats.dropped_leading or stats.dropped_trailing:
        add("")
        add("  UNREAD REFERENCE TEXT (front / back matter)")
        for s in sentences[:min(stats.dropped_leading, 6)]:
            add(f"    - [{s.doc}] {s.text[:70]}")
        if stats.dropped_leading > 6:
            add(f"    … and {stats.dropped_leading - 6} more")
        tail = sentences[len(sentences) - stats.dropped_trailing:]
        for s in tail[:4]:
            add(f"    + [{s.doc}] {s.text[:70]}")
        if stats.dropped_trailing > 4:
            add(f"    … and {stats.dropped_trailing - 4} more")

    add("")
    add("  AUDIO")
    add(f"    transcript      {stats.hyp_tokens} tokens")
    add(f"    anchors         {stats.anchors}"
        f"   (+{stats.gap_recovered} recovered in gaps)")
    add(f"    claimed         {stats.matched_hyp} tokens"
        f"  ({_pct(stats.matched_hyp, stats.hyp_tokens)})")
    if stats.unclaimed_audio_head:
        add("    unclaimed at a file head (publisher intro, credits):")
        for snippet in stats.unclaimed_audio_head[:3]:
            add(f"      {snippet[:88]}")
    if stats.unclaimed_audio_tail:
        add("    unclaimed at a file tail (outro):")
        for snippet in stats.unclaimed_audio_tail[-3:]:
            add(f"      {snippet[:88]}")

    add("")
    add("  OUTPUT")
    add(f"    srt files       {write_stats.files}")
    add(f"    cues            {write_stats.cues}")
    if write_stats.merged_identical:
        add(f"    merged          {write_stats.merged_identical}"
            f"   [identical text under 500 ms apart; the app would merge these]")
    if write_stats.neutralised_markup:
        add(f"    markup fixed    {write_stats.neutralised_markup}"
            f"   [would have been stripped by the app]")
    if getattr(write_stats, "replaced", 0):
        add(f"    replaced        {write_stats.replaced}"
            f"   [existing SRTs overwritten in place]")
    if getattr(write_stats, "retired", None):
        add(f"    retired         {len(write_stats.retired)}"
            f"   [stale SRTs moved aside; their audio had no cues this run]")
        for old, new in write_stats.retired[:5]:
            add(f"      {old} -> {new}")
    if write_stats.empty_files:
        add(f"    no text         {len(write_stats.empty_files)} audio files")
        for name in write_stats.empty_files[:6]:
            add(f"      {name}")
        if len(write_stats.empty_files) > 6:
            add(f"      … and {len(write_stats.empty_files) - 6} more")

    suspect = implausible(cues, lang)
    if suspect:
        add("")
        add(f"  SUSPECT TIMINGS ({len(suspect)} cues, text is right, timing is not)")
        add("    These sat between neighbours with no room between them, so they")
        add("    were squeezed to the minimum cue length. Check if a passage feels off.")
        for c, got, needed in suspect[:8]:
            add(f"    {got:5.2f}s given, ~{needed:4.1f}s needed  "
                f"[{files[c.file_index].name[:28]} @ {format_hms(c.start)}]")
            add(f"          {c.sentence.text[:62]}")
        if len(suspect) > 8:
            add(f"    … and {len(suspect) - 8} more")

    weak = sorted((c for c in matched if c.confidence < 0.75),
                  key=lambda c: c.confidence)[:low_confidence]
    if weak:
        add("")
        add(f"  WEAKEST MATCHES (check these {len(weak)} first)")
        for c in weak:
            add(f"    {c.confidence:4.0%}  {format_hms(c.start)}  "
                f"{c.sentence.text[:60]}")

    add("=" * 72)
    return "\n".join(lines)


def write_json(path: str, book: str, lang: str, files, cues, stats,
               write_stats) -> None:
    payload = {
        "book": book,
        "language": lang,
        "audio_files": [{"name": f.name, "duration": f.duration,
                         "codec": f.codec} for f in files],
        "align": {k: v for k, v in vars(stats).items()},
        "output": {k: v for k, v in vars(write_stats).items()},
        "cues": [
            {"file": files[c.file_index].name, "start": round(c.start, 3),
             "end": round(c.end, 3), "confidence": round(c.confidence, 3),
             "interpolated": c.interpolated, "text": c.sentence.text}
            for c in cues if c is not None
        ],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
