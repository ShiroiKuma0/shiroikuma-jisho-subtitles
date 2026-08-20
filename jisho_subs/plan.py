"""Working out what a directory is, and what should be done to it.

白い熊's library holds three kinds of folder side by side: a book with its EPUB,
a folder of MP3s with subtitles already made for them, and a folder of MP3s with
nothing else. Pointed at the library root, the tool has to tell them apart on its
own — asking for a mode flag on a 127-book run is asking for the wrong flag.

Nothing here touches the disk beyond reading it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

from .audio import AudioFile, discover, natural_key
from .convert import needs_conversion
from .source import find_source
from .srt import companion

#: What the reference text for the subtitles will be taken from.
FROM_EPUB, FROM_SRT, FROM_NOTHING = "epub", "srt", "none"


@dataclass
class BookPlan:
    directory: str
    #: Audio paths, listed but not probed — surveying a whole library must not
    #: cost one ffprobe per file across four thousand of them.
    audio: List[str] = field(default_factory=list)
    reference: str = FROM_NOTHING
    source: Optional[str] = None      #: the EPUB or PDF, when there is one
    to_convert: int = 0
    subtitles: int = 0                #: existing SRTs matching the audio
    shadowed: List[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return os.path.basename(self.directory.rstrip("/"))

    @property
    def tracks(self) -> int:
        return len(self.audio)

    @property
    def action(self) -> str:
        if not self.audio:
            return "no audio"
        if self.to_convert and self.reference != FROM_NOTHING:
            return "convert + subtitles"
        if self.to_convert:
            return "convert"
        if self.reference == FROM_NOTHING:
            return "nothing to do"
        if self.subtitles >= len(self.audio):
            return "nothing to do"
        return "subtitles"

    @property
    def busy(self) -> bool:
        return self.action not in ("nothing to do", "no audio")


def _has_audio(directory: str) -> bool:
    from .audio import AUDIO_EXTS
    try:
        return any(os.path.splitext(f)[1].lower() in AUDIO_EXTS
                   for f in os.listdir(directory)
                   if os.path.isfile(os.path.join(directory, f)))
    except OSError:
        return False


def classify(root: str) -> str:
    """``book``, ``library`` or ``empty``.

    A folder holding a book file, or audio of its own, is a book.  A folder
    whose *children* hold audio is a library.  One child alone is still read as
    a book, since that is what a book directory with its audio in a subfolder
    looks like.
    """
    if find_source(root) or _has_audio(root):
        return "book"
    try:
        children = [os.path.join(root, d) for d in sorted(os.listdir(root))
                    if os.path.isdir(os.path.join(root, d))]
    except OSError:
        return "empty"
    with_audio = [d for d in children if _has_audio(d) or any(
        _has_audio(os.path.join(d, x)) for x in _subdirs(d))]
    if len(with_audio) > 1:
        return "library"
    if len(with_audio) == 1:
        return "book"
    return "empty"


def _subdirs(directory: str) -> List[str]:
    try:
        return [d for d in os.listdir(directory)
                if os.path.isdir(os.path.join(directory, d))]
    except OSError:
        return []


def _audio_paths(directory: str) -> List[str]:
    """Every audio file of a book, by listing alone."""
    from .audio import AUDIO_EXTS

    here = [os.path.join(directory, f) for f in os.listdir(directory)
            if os.path.isfile(os.path.join(directory, f))
            and os.path.splitext(f)[1].lower() in AUDIO_EXTS]
    if here:
        return sorted(here, key=natural_key)
    for sub in sorted(_subdirs(directory), key=natural_key):
        found = _audio_paths(os.path.join(directory, sub))
        if found:
            return found
    return []


def inspect(directory: str) -> BookPlan:
    """What one book directory holds, and therefore what it needs."""
    from .audio import SEEK_ACCURATE

    paths = _audio_paths(directory)
    plan = BookPlan(directory=directory, audio=paths)
    if not paths:
        return plan

    # One track may exist as both an MP3 and its M4B; count tracks, not files.
    by_stem: dict = {}
    for path in paths:
        by_stem.setdefault(os.path.splitext(os.path.basename(path))[0],
                           []).append(path)
    plan.audio = [sorted(v)[0] for v in by_stem.values()]
    plan.to_convert = sum(
        1 for stem, group in by_stem.items()
        if not any(os.path.splitext(p)[1].lower() in SEEK_ACCURATE for p in group))
    plan.shadowed = [p for group in by_stem.values() for p in group
                     if len(group) > 1
                     and os.path.splitext(p)[1].lower() not in SEEK_ACCURATE]
    plan.subtitles = sum(1 for stem in by_stem
                         if os.path.exists(os.path.join(
                             os.path.dirname(paths[0]), stem + ".srt")))

    source = find_source(directory)
    if source:
        plan.reference, plan.source = FROM_EPUB, source
    elif plan.subtitles:
        plan.reference = FROM_SRT
    return plan


def survey(root: str) -> tuple:
    """Return ``(kind, plans)`` for a directory, without changing anything."""
    kind = classify(root)
    if kind == "book":
        return kind, [inspect(root)]
    if kind == "empty":
        return kind, []
    children = sorted((os.path.join(root, d) for d in os.listdir(root)
                       if os.path.isdir(os.path.join(root, d))),
                      key=natural_key)
    return kind, [inspect(d) for d in children]
