"""Whole-book token alignment of the reference text against the transcript.

The received wisdom is that aligning a novel to its audiobook needs chapter
matching, because a global alignment is O(n·m) and blows up.  That is true of
*character*-level dynamic programming — it is what makes subplz depend on
matching audio chapters to text chapters, and what makes it fall over when the
two do not correspond.  At *token* level it simply is not true.  Measured:

    70 000 ref tokens × 69 000 ASR tokens, 12 % word error   difflib  0.8 s
    318 000 × 306 000 characters (Japanese, the worst case)   difflib  44.9 s
                                                              peak RSS 40 MB

So there is no chapter matching here at all.  The book is aligned to the whole
audiobook in one pass, and both kinds of mismatch fall out for free:

* reference text nobody read — front matter, dedications, colophons, the
  section numbers «1»…«60» — matches nothing and is dropped;
* audio nobody wrote down — «Sie hören Lázár … ein Hörbuch des Argon Verlags»,
  «Wydawnictwo Znak … czyta Wojciech Stagenalski» — claims no reference tokens
  and is dropped.

Two tiers: ``difflib`` finds anchors in near-linear memory, then RapidFuzz runs
a proper edit-distance path inside each gap the anchors leave behind.  On real
books those gaps average 4 tokens and never exceeded 45.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from rapidfuzz.distance import Levenshtein

from .asr import Transcript, Word
from .normalize import CHAR_LANGS, tokenize
from .segment import Sentence

#: Anchor blocks shorter than this are noise; character languages need more
#: characters to be as distinctive as a word is.
MIN_ANCHOR = {"word": 3, "char": 6}

#: Beyond this a gap is left to the anchor pass rather than paid for.
MAX_GAP = 4000


@dataclass
class Cue:
    """One sentence, placed on one audio file's timeline."""

    sentence: Sentence
    file_index: int
    start: float
    end: float
    confidence: float
    interpolated: bool = False


@dataclass
class AlignStats:
    ref_tokens: int = 0
    hyp_tokens: int = 0
    matched_hyp: int = 0
    anchors: int = 0
    gap_recovered: int = 0
    sentences: int = 0
    placed: int = 0
    interpolated: int = 0
    dropped_leading: int = 0
    dropped_trailing: int = 0
    dropped_interior: int = 0
    unclaimed_audio_head: List[str] = field(default_factory=list)
    unclaimed_audio_tail: List[str] = field(default_factory=list)


def _build_reference(sentences: Sequence[Sentence], lang: str):
    tokens: List[str] = []
    owner: List[int] = []
    per_sentence: List[int] = []
    for i, s in enumerate(sentences):
        toks = tokenize(s.text, lang)
        per_sentence.append(len(toks))
        tokens.extend(toks)
        owner.extend([i] * len(toks))
    return tokens, owner, per_sentence


def _build_hypothesis(transcripts: Sequence[Transcript], lang: str):
    tokens: List[str] = []
    owner: List[Tuple[int, int]] = []          # (file index, word index)
    words: List[List[Word]] = []
    for fi, tr in enumerate(transcripts):
        words.append(tr.words)
        for wi, w in enumerate(tr.words):
            toks = tokenize(w.text, lang)
            tokens.extend(toks)
            owner.extend([(fi, wi)] * len(toks))
    return tokens, owner, words


def _gap_pass(ref: Sequence[str], hyp: Sequence[str],
              ref_off: int, hyp_off: int) -> List[Tuple[int, int]]:
    """Recover matches inside one gap between anchors."""
    if not ref or not hyp or len(ref) > MAX_GAP or len(hyp) > MAX_GAP:
        return []
    pairs: List[Tuple[int, int]] = []
    try:
        ops = Levenshtein.opcodes(list(ref), list(hyp))
    except Exception:
        return []
    for op in ops:
        if op.tag != "equal":
            continue
        for k in range(op.src_end - op.src_start):
            pairs.append((ref_off + op.src_start + k, hyp_off + op.dest_start + k))
    return pairs


def _match_pairs(ref: List[str], hyp: List[str], min_anchor: int,
                 stats: AlignStats) -> List[Tuple[int, int]]:
    """Anchor pass, then a gap pass between consecutive anchors."""
    # autojunk must stay off: it discards any token occurring in more than 1 %
    # of the sequence, which on a novel means every function word — precisely
    # the connective tissue the alignment runs on.
    sm = difflib.SequenceMatcher(a=ref, b=hyp, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size >= min_anchor]
    stats.anchors = len(blocks)
    if not blocks:
        return []

    pairs: List[Tuple[int, int]] = []
    prev_a = prev_b = None
    for blk in blocks:
        if prev_a is not None:
            recovered = _gap_pass(ref[prev_a:blk.a], hyp[prev_b:blk.b], prev_a, prev_b)
            stats.gap_recovered += len(recovered)
            pairs.extend(recovered)
        pairs.extend((blk.a + k, blk.b + k) for k in range(blk.size))
        prev_a, prev_b = blk.a + blk.size, blk.b + blk.size
    return pairs


def _accept(matched: int, total: int) -> bool:
    """Is a sentence's evidence strong enough to place it?"""
    if total == 0 or matched == 0:
        return False
    if total >= 6 and matched < 2:
        return False
    return matched / total >= 0.34


def align(sentences: Sequence[Sentence], transcripts: Sequence[Transcript],
          lang: str, log=None) -> Tuple[List[Optional[Cue]], AlignStats]:
    """Place every sentence on the audio, or report it as unplaced.

    Returns one entry per sentence, ``None`` where the sentence was never
    spoken.
    """
    log = log or (lambda *a, **k: None)
    stats = AlignStats()
    mode = "char" if lang in CHAR_LANGS else "word"

    ref, ref_owner, ref_counts = _build_reference(sentences, lang)
    hyp, hyp_owner, words = _build_hypothesis(transcripts, lang)
    stats.ref_tokens, stats.hyp_tokens = len(ref), len(hyp)
    stats.sentences = len(sentences)
    if not ref or not hyp:
        return [None] * len(sentences), stats

    log(f"  aligning {len(ref)} reference vs {len(hyp)} transcript tokens")
    pairs = _match_pairs(ref, hyp, MIN_ANCHOR[mode], stats)
    stats.matched_hyp = len(pairs)
    if not pairs:
        return [None] * len(sentences), stats

    # Collect, per sentence, the transcript words its tokens matched.
    hits: Dict[int, List[Tuple[int, int]]] = {}
    for ri, hi in pairs:
        hits.setdefault(ref_owner[ri], []).append(hyp_owner[hi])

    cues: List[Optional[Cue]] = [None] * len(sentences)
    for si, owners in hits.items():
        if not _accept(len(owners), ref_counts[si]):
            continue
        by_file: Dict[int, List[int]] = {}
        for fi, wi in owners:
            by_file.setdefault(fi, []).append(wi)
        # A sentence read across a file boundary belongs to whichever file
        # holds most of it; the tail is clamped at that file's end later.
        fi = max(by_file, key=lambda k: len(by_file[k]))
        widx = by_file[fi]
        start = min(words[fi][w].start for w in widx)
        end = max(words[fi][w].end for w in widx)
        cues[si] = Cue(sentences[si], fi, start, end,
                       len(owners) / max(1, ref_counts[si]))

    placed = [i for i, c in enumerate(cues) if c is not None]
    if not placed:
        return cues, stats
    stats.placed = len(placed)

    # Anything before the first or after the last placed sentence was never
    # read: front matter, colophons, newsletter pages.
    stats.dropped_leading = placed[0]
    stats.dropped_trailing = len(sentences) - placed[-1] - 1

    _interpolate(cues, sentences, placed, ref_counts, stats)
    _enforce_monotonic(cues)
    _record_unclaimed(cues, words, stats)
    return cues, stats


def _interpolate(cues, sentences, placed, ref_counts, stats) -> None:
    """Fill sentences that matched nothing but sit between ones that did.

    A sentence surrounded by placed sentences was read — the aligner just could
    not prove it, usually because ASR mangled a short line.  Dropping it would
    silently remove text from the study material, so it gets a proportional
    share of the interval instead and is flagged in the report.
    """
    for a, b in zip(placed, placed[1:]):
        if b == a + 1:
            continue
        left, right = cues[a], cues[b]
        if left.file_index != right.file_index or right.start <= left.end:
            stats.dropped_interior += b - a - 1
            continue
        span = right.start - left.end
        weights = [max(1, ref_counts[i]) for i in range(a + 1, b)]
        total = sum(weights)
        cursor = left.end
        # A heading that matched nothing was not read aloud — the printed
        # section numbers «1»…«60» are navigation, not narration.  Inventing a
        # cue for one puts a subtitle on screen that the narrator never says,
        # and the app stops playback on it.
        skip = {i for i in range(a + 1, b) if sentences[i].is_heading}
        if skip:
            stats.dropped_interior += len(skip)
            live = [i for i in range(a + 1, b) if i not in skip]
            weights = [max(1, ref_counts[i]) for i in live]
            total = sum(weights) or 1
            for i, weight in zip(live, weights):
                share = span * weight / total
                cues[i] = Cue(sentences[i], left.file_index,
                              cursor, cursor + share, 0.0, True)
                cursor += share
                stats.interpolated += 1
            continue
        for i, weight in zip(range(a + 1, b), weights):
            share = span * weight / total
            cues[i] = Cue(sentences[i], left.file_index,
                          cursor, cursor + share, 0.0, True)
            cursor += share
            stats.interpolated += 1


def _enforce_monotonic(cues: List[Optional[Cue]]) -> None:
    """Keep cues ordered and non-overlapping within each file.

    The app drops its final cue when ``last.end < secondLast.start`` and merges
    cues sharing a timestamp, so out-of-order output loses text silently.
    """
    last_end: Dict[int, float] = {}
    for cue in cues:
        if cue is None:
            continue
        prev = last_end.get(cue.file_index)
        if prev is not None and cue.start < prev:
            cue.start = prev
        if cue.end <= cue.start:
            cue.end = cue.start + 0.30
        last_end[cue.file_index] = cue.end


def _record_unclaimed(cues, words, stats) -> None:
    """Note the audio at each file's head and tail that no sentence claimed."""
    first: Dict[int, float] = {}
    last: Dict[int, float] = {}
    for cue in cues:
        if cue is None:
            continue
        fi = cue.file_index
        first[fi] = min(first.get(fi, cue.start), cue.start)
        last[fi] = max(last.get(fi, cue.end), cue.end)
    for fi, ws in enumerate(words):
        if not ws or fi not in first:
            continue
        head = [w.text for w in ws if w.end <= first[fi]]
        tail = [w.text for w in ws if w.start >= last[fi]]
        if head:
            stats.unclaimed_audio_head.append(" ".join(head[:24]))
        if tail:
            stats.unclaimed_audio_tail.append(" ".join(tail[-24:]))
