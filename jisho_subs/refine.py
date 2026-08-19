"""Snap cue boundaries into the narrator's pauses.

This matters more than raw alignment accuracy.  shiroikuma-jisho pauses playback
at the cue's **own end timestamp** (`reader_audio_toolbar.dart`, auto-pause fires
the moment position leaves the current cue), so a boundary a hair early clips
the last syllable and a hair late leaks the first word of the next sentence.
Whisper's word timestamps are good to a few tens of milliseconds, but they mark
where the *word* ends, not where the reader actually breathes.

Silero VAD gives the speech/silence structure; each cue end is then moved a
little way into the following silence.  Silero ships inside faster-whisper, so
this costs no extra dependency.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Dict, List, Optional, Sequence, Tuple

from .align import Cue

#: How far a boundary may travel to reach a pause.
SEARCH_BACK = 0.60
SEARCH_FORWARD = 1.20
#: How far into the pause the cue should end.
PAUSE_PAD = 0.25
#: A cue may open this far before the first syllable, so nothing is clipped.
LEAD_IN = 0.15
#: A start sitting in silence may travel this far to reach the speech.
MAX_START_TRAVEL = 6.0
#: Speech shorter than this before a pause is a fragment, not a real word.
FRAGMENT = 0.35
#: A pause must be at least this long to be worth moving a start past.
MIN_REAL_PAUSE = 0.50
MIN_CUE = 0.30


def _cache_path(cache_dir: str, path: str) -> str:
    size = os.path.getsize(path)
    h = hashlib.sha1(f"{size}|{os.path.basename(path)}".encode()).hexdigest()[:16]
    return os.path.join(cache_dir, f"vad.{h}.json")


def speech_intervals(path: str, cache_dir: Optional[str] = None,
                     force: bool = False) -> List[Tuple[float, float]]:
    """Speech runs in *path*, as (start, end) seconds."""
    cache_file = _cache_path(cache_dir, path) if cache_dir else None
    if cache_file and not force:
        try:
            with open(cache_file, encoding="utf-8") as fh:
                return [tuple(x) for x in json.load(fh)]
        except (OSError, json.JSONDecodeError):
            pass

    from faster_whisper.vad import get_speech_timestamps, VadOptions

    from .audio import decode

    audio = decode(path, sampling_rate=16000)
    opts = VadOptions(min_silence_duration_ms=250, speech_pad_ms=0)
    raw = get_speech_timestamps(audio, opts)
    out = [(seg["start"] / 16000.0, seg["end"] / 16000.0) for seg in raw]

    if cache_file:
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        tmp = cache_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(out, fh)
        os.replace(tmp, cache_file)
    return out


def _silences(speech: Sequence[Tuple[float, float]],
              duration: float) -> List[Tuple[float, float]]:
    gaps = []
    prev = 0.0
    for start, end in speech:
        if start > prev:
            gaps.append((prev, start))
        prev = max(prev, end)
    if duration > prev:
        gaps.append((prev, duration))
    return gaps


def _snap_end(t: float, gaps: Sequence[Tuple[float, float]]) -> float:
    """Move *t* into the nearest pause, if one is close enough."""
    best = None
    for gs, ge in gaps:
        if ge < t - SEARCH_BACK:
            continue
        if gs > t + SEARCH_FORWARD:
            break
        # Distance from t to this gap (zero when t is already inside it).
        distance = 0.0 if gs <= t <= ge else min(abs(gs - t), abs(ge - t))
        if best is None or distance < best[0]:
            best = (distance, gs, ge)
    if best is None:
        return t
    _d, gs, ge = best
    return min(max(t, gs + min(PAUSE_PAD, (ge - gs) / 2.0)), ge)


def _snap_start(t: float, gaps: Sequence[Tuple[float, float]],
                limit: float) -> float:
    """Pull a start that belongs after a pause forward to the speech itself.

    Whisper sometimes emits a word that is not there.  On Lázár it placed «Ein»
    at 13.010 s, where the audio is digital silence (-78 dB) until 15.6 s; the
    real «Ein blonder Dichter» begins after the pause.  Left alone the cue opens
    on two and a half seconds of nothing, right at the seam where the publisher
    intro ends — so 白い熊 would hear the tail of the credits before the line.

    Two cases move a start: it sits inside a pause, or it sits in a scrap of
    speech that a real pause follows immediately.  The second is the one that
    catches hallucinated words, which land a few milliseconds *before* the pause
    rather than inside it.
    """
    for i, (gs, ge) in enumerate(gaps):
        if gs > t + FRAGMENT:
            break
        inside = gs <= t < ge
        just_before = t < gs <= t + FRAGMENT and (ge - gs) >= MIN_REAL_PAUSE
        if not (inside or just_before):
            continue
        if ge - t > MAX_START_TRAVEL:
            return t
        return min(max(gs, ge - LEAD_IN), limit)
    return t


def refine(cues: Sequence[Optional[Cue]], files, cache_dir: Optional[str] = None,
           force: bool = False, log=None, on_file=None) -> Dict[int, int]:
    """Snap every cue end to a pause.  Returns per-file counts of cues moved."""
    log = log or (lambda *a, **k: None)
    used = sorted({c.file_index for c in cues if c is not None})
    moved: Dict[int, int] = {}

    for fi in used:
        audio = files[fi]
        if on_file is not None:
            on_file("start", audio)
        try:
            speech = speech_intervals(audio.path, cache_dir, force)
        except Exception as exc:
            log(f"  VAD failed on {audio.name}: {exc}")
            if on_file is not None:
                on_file("done", audio)
            continue
        gaps = _silences(speech, audio.duration)
        if not gaps:
            if on_file is not None:
                on_file("done", audio)
            continue
        count = 0
        own = [c for c in cues if c is not None and c.file_index == fi]
        for cue in own:
            if not cue.interpolated:
                moved_start = _snap_start(cue.start, gaps,
                                          cue.end - MIN_CUE)
                if abs(moved_start - cue.start) > 0.01:
                    cue.start = moved_start
                    count += 1
            snapped = _snap_end(cue.end, gaps)
            if abs(snapped - cue.end) > 0.01:
                cue.end = snapped
                count += 1
        # Re-establish ordering: a snap may have pushed one cue past the next.
        own.sort(key=lambda c: c.start)
        for prev, nxt in zip(own, own[1:]):
            if prev.end > nxt.start:
                prev.end = max(prev.start + MIN_CUE, nxt.start)
        for cue in own:
            cue.end = min(cue.end, audio.duration)
            if cue.end - cue.start < MIN_CUE:
                cue.end = min(cue.start + MIN_CUE, audio.duration)
        moved[fi] = count
        if on_file is not None:
            on_file("done", audio)
    return moved
