"""Deciding a Latin-script language by looking its words up.

Script settles Japanese and Russian, and diacritics settle a lot of Polish,
Czech and German. What is left is Latin-script titles carrying nothing but
proper nouns and content words — «Beyond Order», «Greenlights», «Nineteen
eighty-four» — which no hand-written list of function words will ever reach.
A real dictionary does, and the system already ships them.

Entirely optional. Where hunspell dictionaries are not installed this returns
nothing and the caller falls back to its own word lists.
"""

from __future__ import annotations

import functools
import os
import re
from typing import Dict, Optional, Sequence

SEARCH_PATHS = (
    "/usr/share/hunspell",
    "/usr/share/myspell/dicts",
    "/usr/share/myspell",
)

#: Dictionary basenames per language, best first.
CANDIDATES = {
    "en": ("en_US", "en_GB"),
    "de": ("de_DE_frami", "de_DE", "de_AT", "de_CH"),
    "pl": ("pl_PL",),
    "cs": ("cs_CZ",),
    "sk": ("sk_SK",),
    "nl": ("nl_NL", "nl"),
    "fr": ("fr_FR", "fr"),
    "it": ("it_IT",),
    "es": ("es_ES",),
}

#: Used when the .aff file does not declare one.  Polish and Czech dictionaries
#: predate UTF-8 and are Latin-2; the German one is Latin-1.
FALLBACK_ENCODING = {"pl": "iso-8859-2", "cs": "iso-8859-2", "sk": "iso-8859-2"}

_WORD = re.compile(r"^[^\W\d_]+$", re.UNICODE)


def _find(basename: str) -> Optional[str]:
    for directory in SEARCH_PATHS:
        path = os.path.join(directory, basename + ".dic")
        if os.path.exists(path):
            return path
    return None


def _declared_encoding(dic_path: str) -> Optional[str]:
    aff = dic_path[:-4] + ".aff"
    try:
        with open(aff, "rb") as fh:
            for _ in range(40):
                line = fh.readline()
                if not line:
                    break
                if line.upper().startswith(b"SET "):
                    return line.split()[1].decode("ascii", "replace")
    except OSError:
        pass
    return None


def _read(dic_path: str, language: str) -> frozenset:
    encodings = []
    declared = _declared_encoding(dic_path)
    if declared:
        encodings.append(declared)
    encodings += ["utf-8", FALLBACK_ENCODING.get(language, "iso-8859-1")]

    raw = open(dic_path, "rb").read()
    for encoding in encodings:
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        words = set()
        for line in text.split("\n")[1:]:          # first line is a count
            word = line.split("/", 1)[0].strip().lower()
            if word and not word.startswith("#") and len(word) > 2:
                words.add(word)
        if words:
            return frozenset(words)
    return frozenset()


@functools.lru_cache(maxsize=None)
def words_for(language: str) -> frozenset:
    """Every word of a language, or an empty set when no dictionary is installed."""
    for basename in CANDIDATES.get(language, ()):
        path = _find(basename)
        if path:
            return _read(path, language)
    return frozenset()


@functools.lru_cache(maxsize=1)
def available() -> tuple:
    """Languages this machine can actually look up."""
    return tuple(lang for lang in CANDIDATES
                 if any(_find(b) for b in CANDIDATES[lang]))


def score(tokens: Sequence[str], languages: Sequence[str] = ()) -> Dict[str, float]:
    """Fraction of *tokens* each language's dictionary recognises.

    Only real words count — anything with a digit, and anything shorter than
    three letters, is ignored, since those match everywhere and say nothing.
    """
    candidates = [w for w in {t.lower() for t in tokens}
                  if len(w) > 2 and _WORD.match(w)]
    if not candidates:
        return {}
    languages = languages or available()
    out: Dict[str, float] = {}
    for language in languages:
        vocabulary = words_for(language)
        if not vocabulary:
            continue
        hits = sum(1 for w in candidates if w in vocabulary)
        out[language] = hits / len(candidates)
    return out


def best(tokens: Sequence[str], margin: float = 0.15,
         floor: float = 0.34) -> Optional[str]:
    """The language whose dictionary knows these words best, if one clearly does.

    A title's proper nouns are in nobody's dictionary, so the bar is a share of
    the words rather than all of them; the margin is what stops a near-tie —
    common across related languages and loanwords — from being read as an
    answer.
    """
    scores = score(tokens)
    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    winner, top = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    if top >= floor and top - runner_up >= margin:
        return winner
    return None
