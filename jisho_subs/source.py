"""Reference-text extraction: EPUB (primary) and PDF (fallback).

The output is a flat list of :class:`Block` in reading order.  A block is one
leaf-level block element — a paragraph or a heading — never a container.

Two extraction rules here exist because getting them wrong is silent and
poisons the whole alignment:

*Inline-aware text.*  ``get_text()`` is called on the block element, not on its
children, so inline markup contributes no whitespace.  A drop cap
``<span class="initiale">A</span>m Rand`` must come out as ``Am Rand``; a naive
tag-stripper yields ``A m Rand`` and nothing downstream can recover.

*Ruby is removed explicitly.*  Japanese EPUBs annotate readings as
``<ruby>舳先<rt>へさき</rt></ruby>``.  Parsed as XML — the obviously-correct-looking
choice for an XHTML file — that becomes ``舳先へさきに立って``, readings spliced into
the prose.  HTML-mode parsers happen to drop ``<rt>``/``<rp>``, but that is an
accident of libxml2's HTML5 handling rather than a contract, so the tags are
decomposed by hand as well.
"""

from __future__ import annotations

import os
import re
import subprocess
import warnings
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import List, Optional

from bs4 import BeautifulSoup

try:
    from bs4 import XMLParsedAsHTMLWarning
except ImportError:                          # pragma: no cover - older bs4
    XMLParsedAsHTMLWarning = None

# bs4 notices that EPUB content is XHTML and advises parsing it as XML.  Taking
# that advice is the bug: XML mode preserves <rt>, so Japanese ruby readings end
# up spliced into the prose.  HTML mode is the deliberate choice here.
if XMLParsedAsHTMLWarning is not None:
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

#: Leaf-level block elements.  A match is only used when it contains no other
#: block, so nested containers do not produce duplicate text.
BLOCK_TAGS = [
    "p", "h1", "h2", "h3", "h4", "h5", "h6",
    "div", "li", "blockquote", "dd", "dt", "td", "pre",
]

HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

#: Dropped wholesale before text extraction.
DROP_TAGS = ["script", "style", "rt", "rp"]

#: Footnote reference markers: a superscript holding only a number or a dagger.
_NOTE_MARKER = re.compile(r"^[\d\*†‡§¶\[\]()]{1,4}$")

_WS = re.compile(r"[\s　 ]+")


@dataclass
class Block:
    """One paragraph or heading of the reference text."""

    text: str
    doc: str          #: basename of the spine document it came from
    is_heading: bool
    level: int = 0    #: heading level, 0 for body text

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        kind = f"h{self.level}" if self.is_heading else "p"
        return f"<Block {kind} {self.doc} {self.text[:40]!r}>"


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _clean(text: str) -> str:
    """Collapse whitespace, including the ideographic space used to indent
    Japanese paragraphs."""
    return _WS.sub(" ", text).strip()


def _strip_note_markers(soup) -> None:
    """Remove footnote reference markers so stray digits do not enter the text.

    Only superscripts whose entire content is a number or a dagger are removed;
    a superscript carrying real words is left alone.
    """
    for sup in soup.find_all("sup"):
        if _NOTE_MARKER.match(sup.get_text().strip()):
            sup.decompose()
    for a in soup.find_all("a"):
        if a.get("epub:type") == "noteref" or "noteref" in (a.get("class") or []):
            a.decompose()


def _blocks_from_html(raw: str, doc: str) -> List[Block]:
    # HTML mode, never "lxml-xml" — see the module docstring.
    soup = BeautifulSoup(raw, "lxml")
    for tag in DROP_TAGS:
        for el in soup.find_all(tag):
            el.decompose()
    _strip_note_markers(soup)

    body = soup.find("body") or soup
    out: List[Block] = []
    for el in body.find_all(BLOCK_TAGS):
        if el.find(BLOCK_TAGS):
            continue                      # container, not a leaf
        text = _clean(el.get_text())
        if not text:
            continue
        name = el.name
        out.append(Block(text, doc, name in HEADING_TAGS,
                         int(name[1]) if name in HEADING_TAGS else 0))

    if not out:                            # document with no block markup
        text = _clean(body.get_text())
        if text:
            out.append(Block(text, doc, False, 0))
    return out


def _opf_path(z: zipfile.ZipFile) -> str:
    try:
        container = ET.fromstring(z.read("META-INF/container.xml"))
        for el in container.iter():
            if _localname(el.tag) == "rootfile":
                full = el.get("full-path")
                if full:
                    return full
    except (KeyError, ET.ParseError):
        pass
    for name in z.namelist():
        if name.endswith(".opf"):
            return name
    raise ValueError("no OPF found in EPUB")


def _spine(z: zipfile.ZipFile):
    """Return (ordered spine paths, set of nav-document paths, metadata dict)."""
    opf = _opf_path(z)
    root = ET.fromstring(z.read(opf))
    base = os.path.dirname(opf)

    hrefs, props, meta = {}, {}, {}
    order: List[str] = []
    for el in root.iter():
        tag = _localname(el.tag)
        if tag == "item":
            hrefs[el.get("id")] = el.get("href")
            props[el.get("id")] = el.get("properties") or ""
        elif tag == "itemref":
            order.append(el.get("idref"))
        elif tag in ("title", "creator", "language") and el.text:
            meta.setdefault(tag, el.text.strip())

    def resolve(href: str) -> str:
        return os.path.normpath(os.path.join(base, href)) if base else href

    paths, navs = [], set()
    for idref in order:
        href = hrefs.get(idref)
        if not href:
            continue
        path = resolve(href)
        paths.append(path)
        if "nav" in props.get(idref, "").split():
            navs.add(path)
    return paths, navs, meta


def _drop_duplicate_docs(blocks: List[Block], verbose_sink=None) -> List[Block]:
    """Drop spine documents that merely restate text found elsewhere.

    That is what a table of contents *is*: a list of headings that all appear
    again in the body.  Detecting it structurally works in any language and
    needs no ``epub:type`` — which matters, because real books ship a 目次 in
    the reading spine with no metadata marking it at all.

    Without this the aligner produces a false anchor: a spoken chapter title
    matches the contents entry rather than the chapter's own heading.
    """
    by_doc = {}
    for b in blocks:
        by_doc.setdefault(b.doc, []).append(b)
    if len(by_doc) < 2:
        return blocks

    flat = {doc: "".join(_WS.sub("", b.text) for b in bs) for doc, bs in by_doc.items()}
    total = sum(len(v) for v in flat.values())

    dropped = set()
    for doc, bs in by_doc.items():
        # A contents page is always short; skip the expensive test for the rest.
        if len(bs) < 5 or len(flat[doc]) > max(20000, total * 0.05):
            continue
        others = "".join(v for d, v in flat.items() if d != doc)
        hits = 0
        for b in bs:
            needle = _WS.sub("", b.text)
            if len(needle) >= 4 and needle in others:
                hits += 1
        if hits / len(bs) >= 0.6:
            dropped.add(doc)
            if verbose_sink is not None:
                verbose_sink(doc, len(bs), hits / len(bs))

    return [b for b in blocks if b.doc not in dropped]


def load_epub(path: str, sink=None) -> List[Block]:
    z = zipfile.ZipFile(path)
    paths, navs, _meta = _spine(z)
    blocks: List[Block] = []
    for p in paths:
        if p in navs:
            continue                       # EPUB 3 navigation document
        try:
            raw = z.read(p).decode("utf-8", errors="replace")
        except KeyError:
            continue
        blocks.extend(_blocks_from_html(raw, os.path.basename(p)))
    return _drop_duplicate_docs(blocks, sink)


def load_pdf(path: str, sink=None) -> List[Block]:
    """Fallback for books that only exist as PDF.

    ``-raw`` keeps the content-stream order, which is the only mode that reads
    vertical Japanese in the right direction; the layout-aware modes interleave
    the columns into nonsense.
    """
    proc = subprocess.run(["pdftotext", "-raw", path, "-"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"pdftotext failed: {proc.stderr.strip()[:200]}")
    doc = os.path.basename(path)
    blocks = [Block(_clean(line), doc, False, 0)
              for line in proc.stdout.split("\n") if _clean(line)]
    return _drop_duplicate_docs(blocks, sink)


def book_metadata(path: str) -> dict:
    if path.lower().endswith(".epub"):
        try:
            return _spine(zipfile.ZipFile(path))[2]
        except Exception:
            return {}
    return {}


def load_source(path: str, sink=None) -> List[Block]:
    """Load a book's reference text from an EPUB or a PDF."""
    low = path.lower()
    if low.endswith(".epub"):
        return load_epub(path, sink)
    if low.endswith(".pdf"):
        return load_pdf(path, sink)
    raise ValueError(f"unsupported reference format: {path}")


def find_source(directory: str) -> Optional[str]:
    """Pick the book file out of a directory, preferring EPUB over PDF."""
    for ext in (".epub", ".pdf"):
        hits = sorted(f for f in os.listdir(directory) if f.lower().endswith(ext))
        if hits:
            return os.path.join(directory, hits[0])
    return None
