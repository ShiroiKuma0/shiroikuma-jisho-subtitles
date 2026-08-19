"""Sentence segmentation of the reference text.

The book's own text is segmented, never the ASR transcript.  That is the whole
point: the book has correct spelling and correct punctuation, so segmentation is
an easy problem here and a hard one on a transcript.  It also means the subtitle
text 白い熊 reads is the author's, not Whisper's.

pysbd is used because it ships hand-built rule modules for all four target
languages — German with a 115-entry abbreviation list, Japanese with 「」 and
（） protection so dialogue is not cut mid-quote — rather than a generic
punctuation fallback.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from typing import List

import pysbd

from .source import Block

#: pysbd language codes for the languages we support.
SUPPORTED = {"de", "ja", "pl", "ru", "en", "fr", "es", "it", "nl", "zh"}

_ALIASES = {"cz": "cs", "jp": "ja", "gr": "el"}


@dataclass
class Sentence:
    """One subtitle's worth of reference text."""

    text: str
    doc: str
    is_heading: bool
    block_index: int


@functools.lru_cache(maxsize=None)
def _segmenter(lang: str):
    return pysbd.Segmenter(language=lang, clean=False)


def normalise_lang(lang: str) -> str:
    lang = lang.lower()
    lang = _ALIASES.get(lang, lang)
    if lang not in SUPPORTED:
        # pysbd falls back to English rules for anything it does not know, which
        # is a reasonable default for Latin-script languages.
        return "en"
    return lang


def split_block(text: str, lang: str) -> List[str]:
    try:
        parts = _segmenter(lang).segment(text)
    except Exception:
        # pysbd occasionally trips on pathological input; a whole block is a
        # far better fallback than losing it.
        parts = [text]
    return [p.strip() for p in parts if p and p.strip()]


def segment(blocks: List[Block], lang: str) -> List[Sentence]:
    """Turn reference blocks into one-sentence units.

    A heading is kept whole even when it has no terminal punctuation — «Das
    Glaskind» is one spoken unit, and splitting it would produce cues the
    narrator never pauses between.
    """
    lang = normalise_lang(lang)
    out: List[Sentence] = []
    for i, b in enumerate(blocks):
        pieces = [b.text] if b.is_heading else split_block(b.text, lang)
        for piece in pieces:
            out.append(Sentence(piece, b.doc, b.is_heading, i))
    return out
