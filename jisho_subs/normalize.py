"""Normalisation of both sides into comparable token streams.

Matching happens on these tokens, never on the text that ends up in the SRT —
the subtitle always carries the book's exact wording.  So normalisation can be
as aggressive as it likes; its only job is to make the book and the transcript
agree about what counts as "the same word".

Two folds here were found by measurement rather than guessed:

*ß → ss.*  «Also sprach Zarathustra» ships in 1883 orthography (``giebt``,
``thun``, ``Thier``) but with ß already modernised to ss — ``dass`` 141×,
``muß`` never — while Whisper writes modern German *with* ß.  Its opening line
missed on ``dreißig``/``dreissig`` and ``verließ``/``verliess``, which is the
entire reason that book scored 85.6 % against 93–96 % for the others.

*ё → е.*  Russian books print ё; transcripts almost never do.
"""

from __future__ import annotations

import re
import unicodedata
from typing import List

try:                                        # optional, improves number matching
    from num2words import num2words as _num2words
except Exception:                           # pragma: no cover - optional dep
    _num2words = None

#: Japanese is matched character-by-character; every other language by word.
CHAR_LANGS = {"ja", "zh"}

_WORD = re.compile(r"\w+", re.UNICODE)
_DIGITS = re.compile(r"^\d+$")

# Kana, CJK ideographs and ASCII alphanumerics survive Japanese normalisation;
# punctuation, spacing and the marks below do not.
_JA_KEEP = re.compile(r"[ぁ-ゖ一-鿿㐀-䶿0-9a-z]")
_JA_DROP = "ー々ゝゞヽヾ・"

_KATA_TO_HIRA = {c: c - 0x60 for c in range(0x30A1, 0x30F7)}

_APOSTROPHES = dict.fromkeys(map(ord, "'’‘‛`´"), None)

_DE_FOLD = str.maketrans({"ß": "ss"})
_RU_FOLD = str.maketrans({"ё": "е", "Ё": "Е"})


def _expand_number(token: str, lang: str) -> List[str]:
    """Spell a run of digits out, so «1914» can match a written-out year.

    Best effort only.  When it fails the token simply stays a digit run and the
    aligner treats it as a one-token gap, which the anchors either side absorb.
    """
    if _num2words is None or len(token) > 9:
        return [token]
    try:
        spelled = _num2words(int(token), lang=lang)
    except Exception:
        return [token]
    return _WORD.findall(spelled.lower()) or [token]


def tokenize(text: str, lang: str) -> List[str]:
    """Reduce a piece of text to its comparable tokens."""
    if not text:
        return []
    text = unicodedata.normalize("NFKC", text)

    if lang in CHAR_LANGS:
        text = text.translate(_KATA_TO_HIRA).lower()
        return [c for c in text if _JA_KEEP.match(c) and c not in _JA_DROP]

    text = text.translate(_APOSTROPHES).lower()
    if lang == "de":
        text = text.translate(_DE_FOLD)
    elif lang == "ru":
        text = text.translate(_RU_FOLD)

    out: List[str] = []
    for tok in _WORD.findall(text):
        if _DIGITS.match(tok):
            out.extend(_expand_number(tok, lang))
        else:
            out.append(tok)
    return out


def proper_nouns(blocks, lang: str, limit: int = 40) -> List[str]:
    """Mine likely proper nouns to prime Whisper's ``initial_prompt``.

    Without this the narration of «Lázár» comes back as *LASA* or *Lhasa*,
    «Sándor» as *Shandor* — every occurrence a hole in the anchor stream.
    Skipped for Japanese, which has no capitalisation to key off.
    """
    if lang in CHAR_LANGS:
        return []
    counts: dict[str, int] = {}
    for b in blocks:
        # Skip the first word of each sentence: capitalised for position, not
        # because it names anything.
        words = re.findall(r"\b[^\W\d_][\w’'-]*", b.text, re.UNICODE)
        for w in words[1:]:
            if len(w) > 2 and w[0].isupper() and not w.isupper():
                counts[w] = counts.get(w, 0) + 1
    if lang == "de":
        # German capitalises every noun, so frequency alone is not a signal;
        # keep only names that recur often enough to matter.
        floor = 5
    else:
        floor = 2
    ranked = sorted((w for w, n in counts.items() if n >= floor),
                    key=lambda w: -counts[w])
    return ranked[:limit]
