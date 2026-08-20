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
        'cs': 'ces', 'pl': 'pol', 'fr': 'fra', 'es': 'spa', 'it': 'ita',
        'nl': 'nld', 'sk': 'slk', 'uk': 'ukr'}

#: Latin-script languages worth looking a title up in.  Restricted on purpose:
#: with every installed dictionary in play, Dutch and Spanish score highly on
#: English titles and turn a clear answer into a tie.
DICTIONARY_LANGUAGES = ('en', 'de', 'pl', 'cs', 'fr')

#: Words shorter than this match in every language and mean nothing.
DICTIONARY_MIN_WORD = 4

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


#: Scripts that identify a language on sight.  Kana is decisive for Japanese —
#: kanji alone is not, since it is shared with Chinese.
#: Letters only.  The wider kana blocks include CJK punctuation, and a German
#: filename mangled into "Erw・ungen" carries U+30FB — enough, with the whole
#: block matched, to declare Der Zauberberg Japanese.
_SCRIPTS = (
    ('ja', re.compile(r'[ぁ-ゖァ-ヺー]')),        # hiragana and katakana letters
    ('ru', re.compile(r'[Ѐ-ӿ]')),                    # Cyrillic
)

_KANJI = re.compile(r'[一-鿿]')

#: Letters diagnostic *within this set of languages*.  Deliberately excludes
#: the widely shared ones — á, í, é, ó turn up in Hungarian, Spanish and
#: Portuguese names, and "Lázár" in a German book was enough to make the whole
#: thing guess Czech.
_DIACRITICS = {
    'pl': set('ąćęłńśźż'),
    'cs': set('ěřůďťňšžč'),
    'de': set('äöüß'),
}

#: Short, very common words.  Only used to break a tie or to catch a language
#: written without diacritics — "Also sprach Zarathustra" has none at all.
_WORDS = {
    # No word that is also ordinary English — "die", "man", "was", "wie" all
    # occur in English titles and would drag them into German.
    'de': {'der', 'das', 'und', 'von', 'ein', 'eine', 'einen', 'einem',
           'eines', 'dem', 'den', 'nicht', 'sprach', 'für', 'über', 'zur',
           'zum', 'mit', 'auf', 'aus', 'kapitel', 'jenseits', 'böse',
           'geschwister', 'vorrede', 'erstes', 'zweites', 'drittes', 'wird',
           'seele', 'menschen', 'im', 'am', 'vom', 'beim', 'wenn', 'aber',
           'oder', 'doch', 'nur', 'auch', 'noch', 'schon', 'sehr', 'ganz',
           'gegen', 'ohne', 'unter', 'zwischen', 'jungen', 'briefe'},
    # No publisher boilerplate.  "Opening Credits", "Chapter", "Part" and
    # "Book" appear in the tags of German and Japanese audiobooks too, and were
    # enough to label them English.  Nothing that is also an ordinary German
    # word either — "was", "will", "man", "die", "war", "hat".
    # "in", "an", "all" and "her" are German words as well, and adding them
    # was enough to tie "Briefe an einen jungen Dichter" between the two.
    'en': {'the', 'and', 'of', 'to', 'on', 'for', 'with', 'that',
           'his', 'from', 'about', 'how', 'why', 'what', 'after',
           'not', 'into', 'out', 'never', 'been', 'we', 'you',
           'its', 'my', 'our', 'their', 'more', 'most', 'than', 'then',
           'when', 'where', 'who', 'which', 'this', 'these', 'those',
           'are', 'have', 'has', 'had', 'does', 'did', 'can', 'could',
           'should', 'would', 'without', 'against', 'through', 'everything'},
    'pl': {'na', 'przez', 'swój', 'nie', 'się', 'jest', 'rozdział', 'że',
           'oraz', 'aby', 'czy', 'już', 'gdy', 'tylko', 'dla', 'przy',
           'pan', 'pana', 'pani', 'wszystko', 'audiobooki'},
    'cs': {'na', 'se', 'je', 'kapitola', 'který', 'díl', 'část', 'povídka',
           'porodní', 'obhajoba'},
    'ru': {'на', 'не', 'что', 'глава', 'часть', 'том'},
    'ja': {'章', '巻', '第', '編', '話'},   # matched as characters, not words
}


def guess_language(texts) -> Optional[str]:
    """Best guess at a language from names and tags, or None.

    Used only when nothing states the language outright.  MP4 must record
    *something* in its language field, so the choice is between a reasoned guess
    and `und`; a guess is worth making, but a wrong one is worse than none, so
    this returns None unless the evidence is clear.

    Order matters.  Script is decisive where it exists — kana can only be
    Japanese, Cyrillic only Russian here.  Failing that, letters unique to one
    language, then common short words for a language written without any
    (German titles frequently carry no umlaut at all).
    """
    blob = ' '.join(t for t in texts if t)
    if not blob.strip():
        return None
    lowered = blob.lower()

    for language, pattern in _SCRIPTS:
        if pattern.search(blob):
            return language

    scores = {}
    # Kanji without kana is weaker evidence than kana — the script is shared
    # with Chinese — but within this library it means Japanese.
    if _KANJI.search(blob):
        scores['ja'] = scores.get('ja', 0) + 2
    for language, letters in _DIACRITICS.items():
        hits = sum(lowered.count(c) for c in letters)
        if hits:
            scores[language] = scores.get(language, 0) + hits * 3

    # Two letters minimum: single characters are mostly initials, and
    # "Timothy W. Ryback" scoring Polish for its middle initial was enough to
    # tie with English and leave the book undetermined.
    words = {w for w in re.findall(r"[^\W\d_]+", lowered, re.UNICODE)
             if len(w) > 1}
    for language, vocabulary in _WORDS.items():
        hits = len(words & vocabulary)
        if hits:
            scores[language] = scores.get(language, 0) + hits

    if not scores:
        return None
    best, score = max(scores.items(), key=lambda kv: kv[1])
    runner_up = max((v for k, v in scores.items() if k != best), default=0)
    if score <= runner_up:
        return None                    # a tie is not evidence
    # One unmistakable word is enough when nothing competes; two are needed
    # when something does.
    if score >= 2 or runner_up == 0:
        return best
    return None


def before_tags(name: Optional[str]) -> str:
    """The part of a name before ` -- `, where the tags begin.

    Everything after the separator is 白い熊's own tagging — bracket codes, the
    year, and genre words written in Japanese (「小説」, 「哲学」,
    「ルポルタージュ」).  Those last are why the whole name cannot be used: a
    German book's folder ends in kanji.  Everything *before* it is the book's
    own title and author, and is exactly what identifies the language.
    """
    if not name:
        return ''
    head = str(name).split(' -- ', 1)[0]
    return os.path.splitext(head)[0]


def detect_language(info: 'BookInfo', filenames=(), tags=()) -> Optional[str]:
    """Work out a book's language from the names and tags around it.

    Reads the untagged part of the directory and file names — the title and
    author as written — plus whatever the existing tags say.
    """
    if info.language:
        return info.language
    texts = [before_tags(info.directory), info.book, info.author, info.narrator]
    texts += [before_tags(f) for f in filenames]
    for tag in tags:
        if isinstance(tag, dict):
            texts += [tag.get(k) for k in ('title', 'album', 'artist', 'comment')]
    guess = guess_language([demojibake(t) for t in texts if t])
    if guess:
        return guess
    return _dictionary_language(info)


def _dictionary_language(info: 'BookInfo') -> Optional[str]:
    """Look the title's words up, for a title that carries no other signal.

    «Beyond Order», «Greenlights», «Nineteen eighty-four» are plainly English,
    but hold no diacritic, no distinctive script and no function word, so no
    amount of hand-written rules will reach them.  A dictionary does — and the
    system already ships them, so this costs nothing to try and degrades to
    None where none are installed.

    Only the title and author are looked up, not the filenames: track names are
    full of numbers and publisher boilerplate, and author names alone match
    everywhere.
    """
    try:
        from . import dictionaries
    except ImportError:                      # pragma: no cover
        return None
    text = f"{info.book or ''} {info.author or ''}"
    words = [w for w in re.findall(r'[^\W\d_]+', text.lower(), re.UNICODE)
             if len(w) >= DICTIONARY_MIN_WORD]
    if not words:
        return None
    scores = dictionaries.score(words, DICTIONARY_LANGUAGES)
    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    winner, top = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    # A title's proper nouns are in nobody's dictionary, so the bar is a share
    # of the words; the margin is what keeps a near-tie from reading as an
    # answer.
    if top >= 0.34 and top - runner_up >= 0.15:
        return winner
    return None


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

    def with_language(self, language: Optional[str]) -> 'BookInfo':
        """A copy carrying *language* when the folder name did not supply one.

        MP4 stores language in a mandatory field, so a file with none reads
        `und` — there is no way to leave it out.  The fix is therefore not to
        omit the tag but to know the language: the EPUB states it, `-l` states
        it, and only a bare folder of audio leaves it genuinely unknown.
        """
        if self.language or not language:
            return self
        clone = BookInfo(**{k: v for k, v in vars(self).items()})
        clone.language = language
        return clone


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
        return None, 'no title tag, and the filename gave nothing'
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
