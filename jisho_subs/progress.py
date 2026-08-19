"""In-place progress reporting for the stages that take minutes.

Transcribing a 21-hour audiobook is a quarter of an hour of work.  Printing one
line per file floods the terminal with a hundred and eleven near-identical
lines; printing every tenth file leaves five-minute silences where nothing at
all appears.  Neither tells 白い熊 what the tool is actually doing right now.

So: a single line, rewritten in place, naming the file being worked on and
carrying enough numbers to predict the finish.  When output is not a terminal —
a log file, a pipe — it degrades to one plain line at intervals, because
carriage returns in a log are worse than useless.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import time
from typing import Optional

BLOCKS = "▏▎▍▌▋▊▉█"

#: A filename shorter than this identifies nothing, so it is not worth showing.
_MIN_NAME = 14
#: Columns kept for the bar before the filename is allowed to claim space.
_BAR_RESERVE = 16

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """Colour codes occupy no columns; they must not count toward the width."""
    return _ANSI.sub("", text)


def _hms(seconds: float) -> str:
    if seconds < 0 or seconds != seconds or seconds in (float("inf"),):
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _bar(fraction: float, width: int) -> str:
    """A bar with sub-character resolution, so short runs still visibly move."""
    fraction = min(1.0, max(0.0, fraction))
    filled = fraction * width
    whole = int(filled)
    out = "█" * whole
    if whole < width:
        part = filled - whole
        out += BLOCKS[int(part * len(BLOCKS))] if part > 0.02 else " "
        out += " " * (width - whole - 1)
    return out


def shorten(text: str, room: int) -> str:
    """Trim from the middle, keeping both ends.

    Audiobook filenames carry the track number at the front and little else of
    interest after it — `001_111_9783732422098_DEXN82531555.mp3`.  Cutting the
    head to fit leaves `…N82531555.mp3`, which identifies nothing; cutting the
    middle leaves `001_111_97…31555.mp3`, which says which track is running.
    """
    if room <= 1 or len(text) <= room:
        return text
    if room < 8:
        return text[:room - 1] + "…"
    head = (room - 1) * 2 // 3
    tail = room - 1 - head
    return f"{text[:head]}…{text[-tail:]}" if tail else text[:head] + "…"


class Progress:
    """One rewritten line for a stage with a known number of steps."""

    def __init__(self, total: int, label: str, tag: str = "",
                 stream=None, enabled: Optional[bool] = None,
                 min_interval: float = 0.08):
        self.total = max(1, total)
        self.label = label
        self.tag = tag
        self.stream = stream or sys.stderr
        self.min_interval = min_interval
        self.done = 0
        self.weight = 0.0          # e.g. audio seconds completed
        self.started = time.time()
        self._last_draw = 0.0
        self._last_line_len = 0
        self._plain_marker = 0
        if enabled is None:
            enabled = self.stream.isatty() and not os.environ.get("NO_COLOR")
        self.enabled = enabled

    # -- drawing ---------------------------------------------------------

    def _compose(self, detail: str, width: int) -> str:
        """Lay the line out by priority, not by fixed shares.

        What matters most is which file is being worked on — that is the whole
        point of the line — so it is placed before the bar, and the numbers are
        abbreviated rather than the filename dropped.  On a narrow terminal the
        line degrades in this order: speed, elapsed, bar, time-left.
        """
        frac = self.done / self.total
        elapsed = time.time() - self.started
        rate = self.done / elapsed if elapsed > 0 else 0
        remaining = (self.total - self.done) / rate if rate > 0 else float("inf")
        speed = f"{self.weight / elapsed:.0f}×" if self.weight and elapsed > 0 else ""

        head = f"{self.tag}{self.label}"
        head_len = len(self.label) + len(_strip_ansi(self.tag))
        counts = f"{frac * 100:3.0f}% · {self.done}/{self.total}"

        for tier in range(4):
            parts = [counts]
            if tier <= 2:
                parts.append(f"{_hms(remaining)} left")
            if tier <= 1:
                parts.append(f"{_hms(elapsed)} elapsed")
            if tier == 0 and speed:
                parts.append(speed)
            stats = " · ".join(parts)

            room = width - head_len - len(stats) - 3
            if room < 6:
                continue
            name = ""
            if detail:
                # Reserve a usable bar before the name takes the rest.
                want = min(len(detail) + 3, max(0, room - _BAR_RESERVE))
                if want >= _MIN_NAME + 3:
                    name = " · " + shorten(detail, want - 3)
                    room -= len(name)
                elif tier < 3:
                    # Rather than drop the filename, spend a less detailed
                    # stats line on it — knowing which file is running beats
                    # knowing the elapsed time to the second.
                    continue
            bar_width = min(30, room - 2)
            if bar_width < 6:
                if tier < 3:
                    continue
                return f"{head} {stats}{name}"
            return f"{head} ▕{_bar(frac, bar_width)}▏ {stats}{name}"

        return f"{head} {counts}"

    def _draw(self, detail: str, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_draw < self.min_interval:
            return
        self._last_draw = now
        width = shutil.get_terminal_size((100, 24)).columns
        line = self._compose(detail, width - 1)
        pad = " " * max(0, self._last_line_len - len(line))
        self._last_line_len = len(line)
        self.stream.write("\r" + line + pad)
        self.stream.flush()

    def _draw_plain(self, detail: str) -> None:
        """Non-terminal output: a line per decile, never a carriage return."""
        decile = int(10 * self.done / self.total)
        if decile == self._plain_marker and self.done != self.total:
            return
        self._plain_marker = decile
        elapsed = time.time() - self.started
        speed = f", {self.weight / elapsed:.0f}x realtime" if self.weight and elapsed else ""
        self.stream.write(f"{self.tag}{self.label} {self.done}/{self.total}"
                          f" ({_hms(elapsed)} elapsed{speed})\n")
        self.stream.flush()

    # -- api -------------------------------------------------------------

    def advance(self, detail: str = "", amount: int = 1,
                weight: float = 0.0) -> None:
        self.done = min(self.total, self.done + amount)
        self.weight += weight
        if self.enabled:
            self._draw(detail, force=self.done == self.total)
        else:
            self._draw_plain(detail)

    def note(self, detail: str) -> None:
        """Redraw with a new current-item label but no progress."""
        if self.enabled:
            self._draw(detail)

    def close(self, summary: str = "") -> None:
        elapsed = time.time() - self.started
        if self.enabled:
            self.stream.write("\r" + " " * self._last_line_len + "\r")
            self.stream.flush()
        line = f"{self.tag}{self.label} — {self.done}/{self.total} in {_hms(elapsed)}"
        if self.weight and elapsed > 0:
            line += f" ({self.weight / elapsed:.0f}× realtime)"
        if summary:
            line += f"  {summary}"
        self.stream.write(line + "\n")
        self.stream.flush()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class Spinner:
    """An animated line for a blocking call with no measurable progress.

    The whole-book alignment is one ``difflib`` call that offers no hook to
    report from — forty-five seconds of nothing on the Japanese book.  A
    spinner at least distinguishes "working" from "hung".
    """

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, label: str, tag: str = "", stream=None,
                 enabled: Optional[bool] = None):
        self.label, self.tag = label, tag
        self.stream = stream or sys.stderr
        if enabled is None:
            enabled = self.stream.isatty() and not os.environ.get("NO_COLOR")
        self.enabled = enabled
        self._stop = None
        self._thread = None
        self.started = 0.0

    def __enter__(self):
        self.started = time.time()
        if not self.enabled:
            self.stream.write(f"{self.tag}{self.label}…\n")
            self.stream.flush()
            return self
        import threading

        self._stop = threading.Event()

        def run():
            i = 0
            while not self._stop.wait(0.1):
                frame = self.FRAMES[i % len(self.FRAMES)]
                elapsed = _hms(time.time() - self.started)
                self.stream.write(f"\r{self.tag}{frame} {self.label} · {elapsed}\033[K")
                self.stream.flush()
                i += 1

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        if self._stop is not None:
            self._stop.set()
            self._thread.join(timeout=1.0)
            self.stream.write("\r\033[K")
            self.stream.flush()
        return False
