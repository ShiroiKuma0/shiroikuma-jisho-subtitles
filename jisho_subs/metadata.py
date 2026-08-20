"""Deriving book, author, year, language and track titles for the converted audio.

An MP3 audiobook's tags are usually somewhere between unhelpful and absent.  A
survey of 127 books and 4,376 files in ~/〇/[197] オーディオブック found that
after discarding boilerplate only 82 books had a usable track title in their
tags and 8 more in their filenames — the rest said nothing the reader did not
already know.  The directory name, by contrast, is reliable: 白い熊 names every
book `Book, Author -- [tags] (year)`.

So the rule is: trust the folder for book, author, year and language; take the
track title from the tag when it says something, from the filename when it does
not, and leave it unset rather than write a restatement.

What counts as a restatement is deliberately narrow.  Chapter markers —
`Kapitel 1`, `Глава 1`, `第1章`, `Vorrede` — are kept, because that is how the
author divided the book and it names a real part of it.  Only strings that
repeat the book, the author or the folder are dropped.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional

#: Bracket codes 白い熊 uses in file and directory names.
LANGUAGE_CODES = {'227': 'ja', '107': 'de', '007': 'en',
                  '467': 'ru', '677': 'cs', '942': 'pl'}
AUDIOBOOK_CODE = '197'

#: ISO 639-2, for the MP4 language atom.
ISO3 = {'ja': 'jpn', 'de': 'deu', 'en': 'eng', 'ru': 'rus',
        'cs': 'ces', 'pl': 'pol', 'fr': 'fra', 'es': 'spa', 'it': 'ita'}

#: An inverted article belongs to the title: "Achilles trap, the".
ARTICLES = {'the', 'a', 'an', 'der', 'die', 'das', 'le', 'la', 'les', 'el',
            'los', 'il', 'lo', 'de', 'het', 'ein', 'eine'}

#: Words that mark a part of a series, so "Season 2" is never read as a person.
SECTIONING = {'season', 'part', 'volume', 'vol', 'book', 'teil', 'том', 'tom',
              'band', 'series', 'staffel', 'część', 'cz'}

_LEADING_NUM = re.compile(r'^[\s\[\(]*[0-9０-９]{1,4}[\s.．_:：\-—–\)\]]+')
#: Only a *padded* trailing number is a publisher's counter ("… - 001").  A bare
#: one is usually the author's own subdivision: "Часть 1 - 1" is part one,
#: chapter one, and stripping it would throw the chapter away.
_TRAILING_NUM = re.compile(r'[\s._:\-—–]+(?:0[0-9]{1,3}|[0-9]{3,4})$')
_ONLY_DIGITS = re.compile(r'^[\W_]*[0-9]+[\W_]*$')
_NARRATOR = re.compile(r'\s*\((?:исполнитель|читает|gelesen von|read by|'
                       r'narrated by|čte|czyta)\s[^)]*\)', re.I)
_PUBLISHER_ID = re.compile(r'^[A-Z0-9_\-]{8,}$')
_YEAR = re.compile(r'\((\d{3,4})(?:-\d\d-\d\d)?\)')


def demojibake(text: Optional[str]) -> Optional[str]:
    """Undo CP1251 tag bytes that were decoded as Latin-1.

    Russian releases routinely carry this: `Часть 1` arrives as `×àñòü 1` and
    `Лев Николаевич Толстой` as `Ëåâ Íèêîëàåâè÷ Òîëñòîé`.  The repair is to put
    the bytes back and decode them properly.

    It only applies when the result comes out predominantly Cyrillic, so
    ordinary accented Latin — `Anéantir`, `Zoë Schiffer`, `Sněženka`,
    `wędrowcy` — is provably left alone.
    """
    if not text:
        return text
    if not any('À' <= c <= 'ÿ' or c in '×÷¨¸' for c in text):
        return text
    for codec in ('cp1252', 'latin-1'):
        try:
            repaired = text.encode(codec).decode('cp1251')
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        letters = sum(c.isalpha() for c in repaired)
        cyrillic = sum('Ѐ' <= c <= 'ӿ' for c in repaired)
        if letters and cyrillic / letters >= 0.8:
            return repaired
    return text


def _strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFKD', s)
                   if not unicodedata.combining(c))


def _norm(s: Optional[str]) -> str:
    s = _strip_accents((s or '').lower())
    return re.sub(r'[^0-9a-z぀-鿿Ѐ-ӿ]+', '', s)


def _tokens(s: Optional[str]) -> set:
    """Normalised word set — order-insensitive, so a reordered slug still matches."""
    s = _strip_accents((s or '').lower())
    return {w for w in re.split(r'[^0-9a-z぀-鿿Ѐ-ӿ]+', s)
            if len(w) > 1}


@dataclass
class BookInfo:
    """What the directory name says about a book."""

    directory: str
    book: Optional[str] = None
    author: Optional[str] = None
    narrator: Optional[str] = None
    year: Optional[str] = None
    language: Optional[str] = None
    shape: str = 'unknown'
    codes: List[str] = field(default_factory=list)

    @property
    def is_audiobook(self) -> bool:
        return AUDIOBOOK_CODE in self.codes

    @property
    def iso3(self) -> Optional[str]:
        return ISO3.get(self.language or '')


def _looks_like_name(s: str) -> bool:
    words = s.replace('&', ' ').split()
    if not (1 < len(words) <= 5):
        return False
    if any(any(ch.isdigit() for ch in w) for w in words):
        return False                       # "Season 2" is not a person
    if words[0].lower().strip('.') in SECTIONING:
        return False
    return all(w[:1].isupper() or not w[:1].isalpha() for w in words)


def parse_directory(name: str) -> BookInfo:
    """Read `Book, Author -- [codes] (year)` and its several variants."""
    name = demojibake(os.path.basename(name.rstrip('/'))) or ''
    info = BookInfo(directory=name)

    head, sep, meta = name.partition(' -- ')
    if not sep:
        meta = ''
    info.codes = re.findall(r'\[([^\]]+)\]', meta)
    for code in info.codes:
        if code in LANGUAGE_CODES:
            info.language = LANGUAGE_CODES[code]

    m = _YEAR.search(meta)
    if m:
        info.year = m.group(1)
    else:
        m = _YEAR.search(head)
        if m:
            info.year = m.group(1)
            before, after = head[:m.start()].strip(), head[m.end():].strip(' ,')
            if before and after:
                # "Meditations (180) Marcus Aurelius" — the year sits between
                # the title and the author rather than after both of them.
                info.book, info.author = before, after
                info.shape = 'Book (Year) Author'
                return info
            head = head.replace(m.group(0), ' ')
        else:
            m = re.match(r'^(\d{4})\s+', head)
            if m:
                info.year = m.group(1)
                head = head[m.end():]

    m = _NARRATOR.search(head)
    if m:
        info.narrator = re.sub(r'^\s*\(\S+\s+|\)\s*$', '', m.group(0)).strip()
        head = _NARRATOR.sub('', head)
    head = head.strip()

    m = re.match(r'^(.*?)\s*\(([^()]+)\)\s*$', head)
    if m and ',' not in m.group(2):
        info.book, info.author = m.group(1).strip(), m.group(2).strip()
        info.shape = 'Book (Author)'
        return info

    parts = [p.strip() for p in head.split(',') if p.strip()]
    if len(parts) == 1:
        info.book, info.shape = parts[0], 'Book only'
        return info
    while len(parts) > 2 and parts[-1].lower() in ARTICLES:
        parts[-2] = parts[-2] + ', ' + parts.pop()
    if len(parts) == 2 and parts[1].lower() in ARTICLES:
        info.book, info.shape = ', '.join(parts), 'Book only'
        return info

    author, book, shape = parts[-1], ', '.join(parts[:-1]), 'Book, Author'
    if len(parts) >= 3:
        if _looks_like_name(parts[-1]) and _looks_like_name(parts[-2]):
            info.narrator = info.narrator or parts[-1]
            author, book = parts[-2], ', '.join(parts[:-2])
            shape = 'Book, Author, Narrator'
        else:
            shape = 'Book, Subtitle, Author'
    info.book = book
    info.author = re.sub(r'\s+\d+$', '', author).strip() or None
    info.shape = shape
    return info


def clean_track_title(raw: Optional[str], info: BookInfo,
                      album: Optional[str] = None,
                      is_filename: bool = False) -> tuple:
    """Return ``(title, None)`` to keep it, or ``(None, reason)`` to discard.

    Kept: anything naming a part of the work, including bare chapter markers.
    Discarded: only restatements of the book, the author or the folder, plus
    naked track numbers and publisher ids.
    """
    if not raw:
        return None, 'absent'
    title = demojibake(raw).strip()
    if is_filename:
        title = os.path.splitext(title)[0]

    title = _LEADING_NUM.sub('', title).strip()
    removed_book = False
    for ref in filter(None, (info.book, album)):
        before = title
        title = re.sub(re.escape(ref) + r'\s*$', '', title, flags=re.I).strip(' -–—:_.')
        title = re.sub(r'^' + re.escape(ref) + r'\s*[-–—:]\s*', '', title,
                       flags=re.I).strip()
        removed_book = removed_book or title != before
    title = _TRAILING_NUM.sub('', title).strip(' -–—:_.')

    if not title:
        return None, ('the book title, repeated on every track' if removed_book
                      else 'only a track number')
    normalised = _norm(title)
    if not normalised:
        return None, 'no letters'

    for label, ref in (('the book title', info.book), ('the book title', album),
                       ('the author name', info.author),
                       ('the folder name', info.directory)):
        if not ref:
            continue
        reference = _norm(ref)
        if not reference:
            continue
        if normalised == reference:
            return None, f'{label}, repeated on every track'
        if len(normalised) > 8 and (normalised in reference
                                    or reference in normalised):
            return None, f'{label}, repeated on every track'
        # Word order differs between a title and its filename slug, so compare
        # as sets: "The_Achilles_Trap_A" against "Achilles trap, the".  A
        # candidate whose every word already appears in the reference says
        # nothing new, however short it is — "nocni" out of "Nocni wędrowcy".
        here, there = _tokens(title), _tokens(ref)
        if here and there and (here <= there
                               or len(here & there) >= max(2, int(0.8 * len(here)))):
            return None, f'{label}, reordered into a slug'

    if _ONLY_DIGITS.match(title):
        return None, 'only a track number'
    if _PUBLISHER_ID.match(title):
        return None, 'a publisher id'
    return title, None


def build_tags(info: BookInfo, source_tags: Dict[str, str], filename: str,
               index: int, total: int) -> tuple:
    """Tags for one converted file, plus the discard reason if a title was dropped.

    Everything inherited from the source is repaired first, so mojibake cannot
    survive into the M4B even in fields this tool does not derive.
    """
    tags = {k: demojibake(v) for k, v in (source_tags or {}).items()
            if v and k.lower() not in ('title', 'track', 'encoder')}
    album = demojibake((source_tags or {}).get('album'))

    title, reason = clean_track_title((source_tags or {}).get('title'), info, album)
    if not title:
        from_name, name_reason = clean_track_title(filename, info, album,
                                                   is_filename=True)
        if from_name:
            title, reason = from_name, None
        else:
            reason = reason or name_reason

    if title:
        tags['title'] = title
    if info.book:
        tags['album'] = info.book
    if info.author:
        tags['artist'] = info.author
        tags['album_artist'] = info.author
    if info.narrator:
        tags['composer'] = info.narrator
    if info.year:
        tags['date'] = info.year
    if info.iso3:
        tags['language'] = info.iso3
    tags['track'] = f'{index}/{total}'
    tags.setdefault('genre', 'Audiobook')
    return tags, (None if title else reason)
