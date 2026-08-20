"""Command line interface."""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import List, Optional

from . import __version__
from .cuda import ensure_library_path
from .progress import Progress, Spinner


class C:
    RESET = TAG = OK = WARN = ERR = DIM = HEAD = ""

    @classmethod
    def enable(cls):
        cls.RESET, cls.TAG = "\033[0m", "\033[1;33m"
        cls.OK, cls.WARN, cls.ERR = "\033[1;32m", "\033[1;35m", "\033[1;31m"
        cls.DIM, cls.HEAD = "\033[2m", "\033[1;36m"


def _colour_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return sys.stderr.isatty()


def log(msg: str = "") -> None:
    print(f"{C.TAG}[jisho-subs]{C.RESET} {msg}", file=sys.stderr)


class Steps:
    """Numbered stage headings; the count depends on whether we convert."""

    def __init__(self, total: int):
        self.total, self.n = total, 0

    def __call__(self, msg: str) -> None:
        self.n += 1
        print(f"\n{C.TAG}[jisho-subs]{C.RESET} "
              f"{C.DIM}step {self.n} of {self.total}{C.RESET}  "
              f"{C.HEAD}{msg}{C.RESET}", file=sys.stderr)


def step(msg: str) -> None:
    print(f"\n{C.TAG}[jisho-subs]{C.RESET} {C.HEAD}{msg}{C.RESET}", file=sys.stderr)


def die(msg: str, code: int = 1):
    print(f"{C.TAG}[jisho-subs]{C.RESET} {C.ERR}error:{C.RESET} {msg}",
          file=sys.stderr)
    raise SystemExit(code)


# -- input resolution ----------------------------------------------------

def _resolve_inputs(args):
    from .source import find_source
    from .audio import discover

    if getattr(args, "directory_opt", None) and not args.directory:
        args.directory = args.directory_opt

    if args.directory:
        root = os.path.abspath(args.directory)
        if not os.path.isdir(root):
            die(f"not a directory: {root}")
        source = args.epub or find_source(root)
        audio_root = args.audio or root
    else:
        source = args.epub
        audio_root = args.audio
    if not source:
        die("no EPUB or PDF found; pass --epub")
    if not os.path.exists(source):
        die(f"reference file does not exist: {source}")
    if not audio_root or not os.path.isdir(audio_root):
        die("no audio directory; pass --audio or -d")

    shadowed: List[str] = []
    files = discover(audio_root, on_shadow=shadowed.extend)
    args._shadowed = shadowed
    if not files:
        die(f"no audio files under {audio_root}")
    if shadowed:
        # The tool ignores the superseded copies; the app will not.
        log(f"{C.DIM}ignoring {len(shadowed)} superseded file(s) "
            f"(a seek-accurate copy of each exists){C.RESET}")
    return source, files


def _resolve_audio_only(args):
    """Audio alone — `convert` and `probe` have no use for the book text."""
    from .audio import discover

    if getattr(args, "directory_opt", None) and not args.directory:
        args.directory = args.directory_opt
    root = args.audio or args.directory
    if not root or not os.path.isdir(root):
        die("pass a book directory, or --audio")
    shadowed = []
    files = discover(os.path.abspath(root), on_shadow=shadowed.extend)
    args._shadowed = shadowed
    if not files:
        die(f"no audio files under {root}")
    if shadowed:
        log(f"{C.DIM}ignoring {len(shadowed)} superseded file(s) "
            f"(a seek-accurate copy of each exists){C.RESET}")
    return files


def _confirm_delete(count: int, where: str, assume_yes: bool,
                    already_warned: bool = False) -> bool:
    """Ask before removing audio, defaulting to no.

    Same shape as srt-sentence-split.py's destructive mode: a loud warning, and
    a bare Enter means *don't*.  `-d` used to mean `--dir`, so a stale command
    line must not quietly delete a book.
    """
    if already_warned:
        # A library run warns once, up front, for the whole plan.  Repeating
        # the block for every book is the noise 白い熊 asked to be rid of.
        return True
    log("")
    log(f"{C.ERR}*** -d given: {count} MP3 file(s) in {where}")
    log(f"    will be PERMANENTLY DELETED (each one only after its M4B is")
    log(f"    verified to be real audio of the same length) ***{C.RESET}")
    if assume_yes:
        log(f"{C.DIM}-y given, proceeding without asking{C.RESET}")
        return True
    print(f"{C.TAG}[jisho-subs]{C.RESET} {C.TAG}Delete them?{C.RESET} [y/N] ",
          file=sys.stderr, end="", flush=True)
    try:
        answer = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        answer = ""
    if answer in ("y", "yes"):
        return True
    log(f"{C.DIM}keeping the MP3s.{C.RESET}")
    return False


def _check_track_numbering(files) -> None:
    """Warn when the book's track totals disagree.

    A book converted across two runs carries the totals each run knew about —
    five files tagged "n/5" and a sixth tagged "6/6" — because tags written
    earlier cannot be revised without re-converting.  Players sort on this, so
    say so rather than leave it to be discovered on the phone.
    """
    from .audio import read_tags

    totals = {}
    for f in files:
        raw = (read_tags(f.path).get("track") or "").strip()
        if "/" in raw:
            totals.setdefault(raw.split("/", 1)[1].strip(), []).append(f.name)
    if not totals:
        return
    expected = str(len(files))
    if len(totals) > 1 or expected not in totals:
        log(f"  {C.WARN}track numbering is inconsistent: "
            f"{', '.join('n/' + k for k in sorted(totals))} "
            f"across {len(files)} tracks{C.RESET}")
        log(f"  {C.DIM}earlier runs tagged what they knew; --force re-converts "
            f"and renumbers the whole book{C.RESET}")


def _book_numbering(files, stale):
    """Track numbers counted across the whole book, not this batch."""
    from .convert import numbering
    return numbering(list(files) + list(stale))


def _shadowed_sources(args):
    """MP3s that discover() hid because an M4B of the same name exists.

    They are still on disk, so both `-d` and `--force` have to be able to reach
    them: otherwise re-running a converted book finds nothing to work on and
    silently does nothing at all.
    """
    from .audio import SEEK_ACCURATE, probe

    out = []
    for path in getattr(args, "_shadowed", []) or []:
        if os.path.splitext(path)[1].lower() in SEEK_ACCURATE:
            continue
        info = probe(path)
        if info is not None:
            out.append(info)
    return out


def _conversion_candidates(args, files):
    """What to convert — plus, with --force, what was already converted once."""
    from .convert import needs_conversion

    stale = needs_conversion(files)
    if getattr(args, "force", False):
        seen = {f.path for f in stale}
        stale += [f for f in _shadowed_sources(args) if f.path not in seen]
        stale.sort(key=lambda f: f.path)
    return stale


def _deletion_candidates(args, stale):
    """Every MP3 that now has an M4B — freshly converted or from an earlier run.

    Once a book has been converted, discover() shadows its MP3s, so they are no
    longer in the working set.  They are still on disk, though, and -d has to be
    able to remove them on a later run rather than only in the same breath as
    the conversion.
    """
    seen = {f.path for f in stale}
    return list(stale) + [f for f in _shadowed_sources(args) if f.path not in seen]


def _delete_mp3s(args, stale, out_dir: str, assume_yes: bool) -> None:
    from .convert import delete_sources

    targets = _deletion_candidates(args, stale)
    if not targets:
        log(f"{C.DIM}-d given, but there are no MP3s left to delete{C.RESET}")
        return
    if not _confirm_delete(len(targets), out_dir, assume_yes,
                           getattr(args, "_warned_delete", False)):
        return
    deleted, kept = delete_sources(targets, out_dir, log=lambda m: log(m))
    log(f"  {C.OK}deleted {len(deleted)} MP3 file(s){C.RESET}")
    if kept:
        log(f"  {C.WARN}kept {len(kept)} whose replacement did not verify{C.RESET}")
        for name, why in kept[:5]:
            log(f"    {name}: {why}")


def _report_tags(result) -> None:
    """Say what was written into the M4Bs, and what was left out."""
    info = result.info
    if info is None:
        return
    for method, n in sorted((result.methods or {}).items(), key=lambda kv: -kv[1]):
        mark = C.OK if "losslessly" in method else C.WARN
        log(f"  {mark}{n} file(s) {method}{C.RESET}")
    log("")
    log(f"  {C.HEAD}tags written from the folder name{C.RESET}")
    for label, value in (("album", info.book), ("artist", info.author),
                         ("composer", info.narrator), ("date", info.year),
                         ("language", info.iso3)):
        if value:
            log(f"    {label:<9} {value}")
    if not info.iso3:
        log(f"    {C.DIM}language  unknown — MP4 requires the field, so it "
            f"reads 'und'; pass -l to set it{C.RESET}")
    total = result.track_total or (len(result.made) + len(result.skipped))
    log(f"    {'track':<9} N/{total}")
    kept = len(result.titled or [])
    dropped = len(result.discarded or [])
    if kept:
        log(f"    {'title':<9} {kept} track(s), e.g. "
            f"{C.DIM}{result.titled[0][1][:44]}{C.RESET}")
    if dropped:
        import collections
        why = collections.Counter(r for _, r in result.discarded)
        log(f"  {C.DIM}no title on {dropped} track(s) — nothing there but:{C.RESET}")
        for reason, n in why.most_common(4):
            log(f"    {C.DIM}{n:>4}  {reason}{C.RESET}")


def _rediscover(directory: str):
    from .audio import discover
    files = discover(directory)
    if not files:
        die(f"conversion produced nothing in {directory}")
    return files


def _resolve_language(args, source: str) -> str:
    from .source import book_metadata
    from .segment import normalise_lang

    if args.lang:
        return normalise_lang(args.lang)
    meta = book_metadata(source)
    declared = (meta.get("language") or "").split("-")[0].strip().lower()
    if declared:
        log(f"language not given; using {C.HEAD}{declared}{C.RESET} "
            f"from the book's metadata")
        return normalise_lang(declared)

    # Failing that, the folder's own bracket code — [107] German, [942] Polish
    # and so on.  Only a few directories carry one, but it costs nothing to look.
    from .metadata import parse_directory
    if args.directory:
        info = parse_directory(os.path.abspath(args.directory))
        if info.language:
            log(f"language not given; using {C.HEAD}{info.language}{C.RESET} "
                f"from the folder's [{[c for c in info.codes if c in __import__('jisho_subs.metadata', fromlist=['x']).LANGUAGE_CODES][0]}] code")
            return normalise_lang(info.language)
    die("could not determine the language; pass -l/--lang")


# -- commands ------------------------------------------------------------

def cmd_probe(args) -> int:
    from .audio import format_hms
    source, files = _resolve_inputs(args)
    lang = _resolve_language(args, source)
    log(f"reference : {os.path.basename(source)}")
    log(f"language  : {lang}")
    log(f"audio     : {len(files)} files, "
        f"{format_hms(sum(f.duration for f in files))}")
    for f in files[:8]:
        log(f"  {f.name}  {format_hms(f.duration)}  [{f.codec} in {f.container}]")
    if len(files) > 8:
        log(f"  … and {len(files) - 8} more")
    return 0


def _resolve_reference_only(args) -> str:
    """The reference file alone — `sentences` needs no audio."""
    from .source import find_source

    if getattr(args, "directory_opt", None) and not args.directory:
        args.directory = args.directory_opt
    if args.epub:
        if not os.path.exists(args.epub):
            die(f"reference file does not exist: {args.epub}")
        return args.epub
    if not args.directory or not os.path.isdir(args.directory):
        die("pass --epub, or -d pointing at the book directory")
    source = find_source(os.path.abspath(args.directory))
    if not source:
        die(f"no EPUB or PDF in {args.directory}")
    return source


def cmd_sentences(args) -> int:
    from .source import load_source
    from .segment import segment

    source = _resolve_reference_only(args)
    lang = _resolve_language(args, source)
    dropped: List[tuple] = []
    blocks = load_source(source, lambda d, n, r: dropped.append((d, n, r)))
    sentences = segment(blocks, lang)
    for d, n, r in dropped:
        log(f"{C.WARN}dropped duplicate document{C.RESET} {d} "
            f"({n} blocks, {r:.0%} found elsewhere)")
    log(f"{len(blocks)} blocks -> {len(sentences)} sentences")
    for i, s in enumerate(sentences, 1):
        mark = "#" if s.is_heading else " "
        print(f"{i:5d}{mark} [{s.doc}] {s.text}")
    return 0


def cmd_convert(args) -> int:
    from .convert import convert, have_ffmpeg, needs_conversion, target_dir

    files = _resolve_audio_only(args)
    stale = _conversion_candidates(args, files)
    if not stale:
        if args.force:
            # Nothing needs converting, but --force still means redo the work:
            # rewrite the tags in place.  This is the only way to correct a
            # book whose MP3s have already been deleted.
            log(f"all {len(files)} files are already seek-accurate; "
                f"{C.HEAD}--force re-tags them in place{C.RESET}")
            from .convert import retag
            bar = Progress(len(files), "re-tagging",
                           tag=f"{C.TAG}[jisho-subs]{C.RESET} ")
            result = retag(files, positions=_book_numbering(files, []),
                           jobs=args.jobs,
                           language=getattr(args, "lang", None),
                           on_start=lambda f: bar.note(f.name),
                           on_done=lambda f: bar.advance(f.name))
            bar.close(f"{len(result.made)} re-tagged")
            for name, err in result.failed[:5]:
                log(f"  {C.ERR}failed{C.RESET} {name}: {err}")
            _report_tags(result)
            _check_track_numbering(_resolve_audio_only(args))
            return 1 if result.failed else 0
        log(f"{C.OK}nothing to convert{C.RESET} — all "
            f"{len(files)} files are already seek-accurate")
        if args.delete_mp3:
            where = os.path.dirname(os.path.abspath(files[0].path))
            _delete_mp3s(args, [], where, args.yes)
        return 0
    if not have_ffmpeg():
        die("ffmpeg not found on PATH")
    out_dir = args.convert_to or target_dir(stale)
    log(f"converting {len(stale)} files → {out_dir}")
    bar = Progress(len(stale), "converting", tag=f"{C.TAG}[jisho-subs]{C.RESET} ")
    result = convert(stale, out_dir, jobs=args.jobs, force=args.force,
                     reencode=args.reencode,
                     positions=_book_numbering(files, stale),
                     language=getattr(args, "lang", None),
                     on_start=lambda f: bar.note(f.name),
                     on_done=lambda f: bar.advance(f.name, weight=f.duration))
    bar.close(f"{len(result.made)} converted"
              + (f", {len(result.skipped)} already present" if result.skipped else ""))
    for name, err in result.failed:
        log(f"  {C.ERR}failed{C.RESET} {name}: {err}")
    if result.failed:
        return 1
    _report_tags(result)
    _check_track_numbering(_resolve_audio_only(args))
    if args.delete_mp3:
        _delete_mp3s(args, stale, result.out_dir, args.yes)
    else:
        log(f"{C.DIM}the MP3s are still there; -d deletes them once each M4B "
            f"verifies{C.RESET}")
    return 0


def cmd_lint(args) -> int:
    from .srt import lint
    targets: List[str] = []
    for path in args.paths:
        if os.path.isdir(path):
            targets.extend(sorted(os.path.join(path, f)
                                  for f in os.listdir(path) if f.endswith(".srt")))
        else:
            targets.append(path)
    if not targets:
        die("no .srt files to check")
    bad = 0
    for path in targets:
        problems = lint(path)
        if problems:
            bad += 1
            log(f"{C.ERR}{os.path.basename(path)}{C.RESET}")
            for p in problems[:10]:
                log(f"    {p}")
            if len(problems) > 10:
                log(f"    … and {len(problems) - 10} more")
    if bad:
        log(f"{C.ERR}{bad}/{len(targets)} files have problems{C.RESET}")
        return 1
    log(f"{C.OK}all {len(targets)} files satisfy the app's SRT contract{C.RESET}")
    return 0


def _reference_from_srt(plan, log_fn) -> list:
    """Use the folder's existing cues as the reference text.

    A folder of MP3s with subtitles has everything needed except a book: the
    cues *are* the sentences. Feeding them in where an EPUB's sentences would go
    means the same alignment re-places them against the converted audio, which
    is a genuine resync rather than a constant offset — and it repairs an SRT
    that was already drifting.
    """
    from .segment import Sentence
    from .srt import companion, read_cues

    sentences, sources = [], 0
    for audio in plan.audio:
        path = companion(audio)
        if not path:
            continue
        cues = read_cues(path)
        if not cues:
            continue
        sources += 1
        name = os.path.basename(path)
        for text in cues:
            sentences.append(Sentence(text, name, False, len(sentences)))
    log_fn(f"  {len(sentences)} cues from {sources} existing SRT file(s)")
    return sentences


def _language_for(args, plan, files):
    """The book's language: stated, declared by the EPUB, or guessed."""
    from .metadata import detect_language, parse_directory
    from .segment import normalise_lang
    from .source import book_metadata
    from .audio import read_tags

    if getattr(args, "lang", None):
        return normalise_lang(args.lang)
    if plan.source:
        declared = (book_metadata(plan.source).get("language") or "")
        declared = declared.split("-")[0].strip().lower()
        if declared:
            return normalise_lang(declared)
    info = parse_directory(os.path.dirname(os.path.abspath(plan.audio[0])))
    sample = files[:3]
    guess = detect_language(info, [f.name for f in sample],
                            [read_tags(f.path) for f in sample])
    if guess:
        log(f"  language not stated; {C.HEAD}{guess}{C.RESET} from the folder "
            f"and file names")
        return normalise_lang(guess)
    die("could not determine the language; pass -l/--lang")


def _run_one_book(args, plan) -> bool:
    """The whole pipeline for one book.  Returns True if anything was done."""
    from .align import align
    from .asr import Transcriber, default_cache_dir
    from .audio import discover, format_hms
    from .convert import convert, have_ffmpeg, needs_conversion, target_dir
    from .normalize import proper_nouns
    from .plan import FROM_EPUB, FROM_NOTHING, FROM_SRT
    from .segment import segment
    from .source import load_source
    from .srt import write_for_files
    from . import report as report_mod

    started = time.time()
    shadowed: List[str] = []
    files = discover(plan.directory, on_shadow=shadowed.extend)
    args._shadowed = shadowed
    if not files:
        return False
    lang = _language_for(args, plan, files)

    stale = _conversion_candidates(args, files) if args.convert else []
    if stale and args.dry_run:
        log(f"{C.WARN}dry run: not converting {len(stale)} MP3 file(s){C.RESET}")
        stale = []
    subtitles = plan.reference != FROM_NOTHING
    step = Steps((1 if stale else 0) + (5 if subtitles else 0) or 1)

    if stale:
        already = len(files) - len(stale)
        step(f"converting {len(stale)} of {len(files)} tracks to M4B"
             if already else f"converting {len(stale)} tracks to M4B")
        if not have_ffmpeg():
            die("ffmpeg is needed to convert MP3 to M4B; pass --keep-mp3 to skip")
        out_dir = args.convert_to or target_dir(stale)
        cbar = Progress(len(stale), "converting", tag=f"{C.TAG}[jisho-subs]{C.RESET} ")
        result = convert(stale, out_dir, jobs=args.jobs, force=args.force,
                         reencode=args.reencode, language=lang,
                         positions=_book_numbering(files, stale),
                         on_start=lambda f: cbar.note(f.name),
                         on_done=lambda f: cbar.advance(f.name, weight=f.duration))
        cbar.close(f"{len(result.made)} converted")
        for name, err in result.failed[:5]:
            log(f"  {C.ERR}failed{C.RESET} {name}: {err}")
        if result.failed:
            die(f"{len(result.failed)} file(s) failed to convert")
        _report_tags(result)
        if args.delete_mp3:
            _delete_mp3s(args, stale, result.out_dir, args.yes)
        files = _rediscover(result.out_dir)
        _check_track_numbering(files)

    if not subtitles:
        log(f"  {C.OK}✓ {len(files)} tracks are M4B; no book or subtitles here, "
            f"so nothing further{C.RESET}")
        return True

    dropped_docs: List[tuple] = []
    if plan.reference == FROM_EPUB:
        step(f"reading {os.path.basename(plan.source)}")
        blocks = load_source(plan.source, lambda d, n, r: dropped_docs.append((d, n, r)))
        for d, n, r in dropped_docs:
            log(f"  {C.WARN}dropped{C.RESET} {d}  ({n} blocks, {r:.0%} of it "
                f"appears elsewhere — a contents page)")
        sentences = segment(blocks, lang)
        log(f"  {len(blocks)} blocks -> {len(sentences)} sentences")
        names = proper_nouns(blocks, lang)
    else:
        step("reading the existing subtitles as the reference text")
        sentences = _reference_from_srt(plan, log)
        names = []
    if not sentences:
        log(f"  {C.WARN}no reference text; skipping subtitles{C.RESET}")
        return True

    step(f"transcribing {len(files)} audio files "
         f"({format_hms(sum(f.duration for f in files))})")
    prompt = ", ".join(names) if names else None
    if prompt:
        log(f"  priming Whisper with {len(names)} names: "
            f"{C.DIM}{prompt[:70]}…{C.RESET}")
    cache_dir = args.cache_dir or default_cache_dir()
    bar = Progress(len(files), "transcribing", tag=f"{C.TAG}[jisho-subs]{C.RESET} ")
    tr = Transcriber(model_name=args.model, device=args.device,
                     batch_size=args.batch_size, beam_size=args.beam_size,
                     cache_dir=cache_dir, initial_prompt=prompt,
                     log=lambda m: bar.note(m.strip()))
    transcripts = []
    for f in files:
        bar.note(f.name)
        transcripts.append(tr.transcribe(f, lang, force=args.force))
        bar.advance(f.name, weight=0.0 if tr.last_cached else f.duration)
    bar.close(f"{tr.cache_hits} from cache" if tr.cache_hits else "")

    step("aligning")
    with Spinner("aligning the reference against the audio",
                 tag=f"{C.TAG}[jisho-subs]{C.RESET} "):
        cues, stats = align(sentences, transcripts, lang, log=lambda m: log(m))
    if not stats.placed:
        log(f"  {C.ERR}nothing matched — skipping this book{C.RESET}")
        return True
    log(f"  placed {stats.placed}/{len(sentences)}, {stats.anchors} anchors, "
        f"{100.0 * stats.matched_hyp / max(1, stats.hyp_tokens):.1f}% of audio claimed")

    if args.refine:
        step("snapping cue ends into the narrator's pauses")
        from .refine import refine
        used = len({c.file_index for c in cues if c is not None})
        vbar = Progress(used, "snapping", tag=f"{C.TAG}[jisho-subs]{C.RESET} ")

        def on_file(phase, audio):
            if phase == "start":
                vbar.note(audio.name)
            else:
                vbar.advance(audio.name, weight=audio.duration)

        moved = refine(cues, files, cache_dir=cache_dir, force=args.force,
                       log=lambda m: vbar.note(m.strip()), on_file=on_file)
        vbar.close(f"{sum(moved.values())} cue ends adjusted")

    step("writing subtitles")
    if args.dry_run:
        from .srt import WriteStats
        log(f"  {C.WARN}dry run — nothing written{C.RESET}")
        write_stats = WriteStats()
    else:
        wbar = Progress(len(files), "writing", tag=f"{C.TAG}[jisho-subs]{C.RESET} ")
        write_stats = write_for_files(
            cues, files, args.out,
            log=lambda m: (log(m) if args.verbose else wbar.note(m.strip())),
            on_file=lambda name: wbar.advance(name))
        wbar.close()
        log(f"  {C.OK}{write_stats.files} SRT files, {write_stats.cues} cues{C.RESET}")

    book = plan.name if plan.reference != FROM_EPUB else \
        os.path.splitext(os.path.basename(plan.source))[0]
    text = report_mod.build(book, lang, plan.source or plan.directory, files,
                            sentences, cues, stats, write_stats, dropped_docs)
    if args.report:
        with open(args.report, "a" if getattr(args, "_appending", False) else "w",
                  encoding="utf-8") as fh:
            fh.write(text + "\n")
        args._appending = True
    elif not getattr(args, "_quiet_report", False):
        print(text)
    if args.json:
        report_mod.write_json(args.json, book, lang, files, cues, stats, write_stats)
    log(f"{C.OK}done in {time.time() - started:.0f}s{C.RESET}")
    return True


def _show_plan(root: str, plans) -> int:
    """Print what a library run intends to do.  Returns the number of busy books."""
    import collections

    counts = collections.Counter(p.action for p in plans)
    tracks = sum(p.tracks for p in plans if p.busy)
    log("")
    log(f"  {C.HEAD}{len(plans)} directories under {os.path.basename(root)}"
        f"{C.RESET}")
    for action in ("convert + subtitles", "convert", "subtitles",
                   "nothing to do", "no audio"):
        if counts.get(action):
            colour = C.DIM if action in ("nothing to do", "no audio") else C.OK
            log(f"    {colour}{counts[action]:>4}  {action}{C.RESET}")
    log("")
    for p in plans:
        if not p.busy:
            continue
        log(f"    {C.OK}{p.action:<20}{C.RESET} {p.tracks:>4} tracks  "
            f"{C.DIM}{p.reference:<5}{C.RESET} {p.name[:56]}")
    busy = sum(1 for p in plans if p.busy)
    log("")
    log(f"  {busy} book(s) to process, {tracks} tracks in total")
    return busy


def cmd_run(args) -> int:
    from .plan import inspect, survey

    if getattr(args, "directory_opt", None) and not args.directory:
        args.directory = args.directory_opt
    root = args.directory or args.audio
    if not root or not os.path.isdir(root):
        die("pass a book directory, a library directory, or --epub with --audio")
    root = os.path.abspath(root)

    if args.epub or args.audio:
        # An explicit source or audio directory means one book, stated outright.
        plan = inspect(root)
        if args.epub:
            from .plan import FROM_EPUB
            plan.source, plan.reference = args.epub, FROM_EPUB
        return 0 if _run_one_book(args, plan) else 1

    kind, plans = survey(root)
    if kind == "empty":
        die(f"no audio found under {root}")
    if kind == "book":
        return 0 if _run_one_book(args, plans[0]) else 1

    step_all = Steps(1)
    step_all(f"surveying {os.path.basename(root)}")
    busy = _show_plan(root, plans)
    if not busy:
        log(f"  {C.OK}nothing to do{C.RESET}")
        return 0
    if args.delete_mp3:
        log("")
        log(f"  {C.ERR}*** -d given: every MP3 in these {busy} book(s) will be")
        log(f"      PERMANENTLY DELETED once its M4B is verified ***{C.RESET}")
    if args.dry_run:
        # On a library the plan *is* the rehearsal.  Running a full
        # alignment for every book to then write nothing would take hours
        # to say what the table above already said.
        log(f"  {C.WARN}dry run — the plan above is what would happen; "
            f"nothing was touched{C.RESET}")
        return 0
    if not args.yes:
        print(f"{C.TAG}[jisho-subs]{C.RESET} {C.TAG}Proceed?{C.RESET} [y/N] ",
              file=sys.stderr, end="", flush=True)
        try:
            answer = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            answer = ""
        if answer not in ("y", "yes"):
            log(f"{C.DIM}nothing done.{C.RESET}")
            return 0

    # Asked once, up front; never again per book, and never warned again.
    args.yes = True
    args._warned_delete = True
    args._quiet_report = True
    done = failed = 0
    for i, plan in enumerate([p for p in plans if p.busy], 1):
        log("")
        log(f"{C.HEAD}━━ {i}/{busy}  {plan.name}{C.RESET}")
        try:
            _run_one_book(args, plan)
            done += 1
        except SystemExit as exc:
            failed += 1
            log(f"  {C.ERR}skipped: {exc}{C.RESET}")
    log("")
    log(f"{C.OK}library finished — {done} book(s) processed"
        f"{f', {failed} skipped' if failed else ''}{C.RESET}")
    return 1 if failed else 0


# -- argument parsing ----------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="shiroikuma-jisho-subtitles",
        description="Turn an EPUB and its audiobook into one-sentence-per-cue "
                    "SRT files for shiroikuma-jisho.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  shiroikuma-jisho-subtitles -s                    # set up or check this machine
  shiroikuma-jisho-subtitles ~/tmp/subtitles/1     # language read from the EPUB
  shiroikuma-jisho-subtitles ~/books/L -d          # ...then delete the MP3s
  shiroikuma-jisho-subtitles sentences ~/books/L   # just the reference text
  shiroikuma-jisho-subtitles probe ~/books/L       # what would be used
  shiroikuma-jisho-subtitles lint ~/books/L/audio  # check written SRTs

The full manual, including what -d deletes and what protects you from it,
is in the wrapper: shiroikuma-jisho-subtitles -h
""")
    p.add_argument("--version", action="version", version=f"jisho-subs {__version__}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("directory", nargs="?",
                        help="book directory holding the EPUB and the audio")
    common.add_argument("--dir", dest="directory_opt",
                        help="same as the positional argument")
    common.add_argument("--epub",
                        help="reference EPUB or PDF (overrides the directory)")
    common.add_argument("--audio",
                        help="directory of audio files (overrides the directory)")
    common.add_argument("-l", "--lang",
                        help="language code; read from the EPUB when omitted")

    sub = p.add_subparsers(dest="command")

    run = sub.add_parser("run", parents=[common], help="the full pipeline (default)")
    run.add_argument("-o", "--out",
                     help="where to write SRTs (default: beside each audio file, "
                          "which is what makes the app pair them automatically)")
    run.add_argument("--model", default="large-v3", help="Whisper model")
    run.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    run.add_argument("--batch-size", type=int, default=32)
    run.add_argument("--beam-size", type=int, default=1)
    run.add_argument("--cache-dir")
    run.add_argument("--no-refine", dest="refine", action="store_false",
                     help="skip snapping cue ends into pauses")
    run.add_argument("--keep-mp3", dest="convert", action="store_false",
                     help="do not convert MP3 to M4B first (the app cannot seek "
                          "MP3, so auto-pause will land on the wrong sentence)")
    run.add_argument("--convert-to",
                     help="where converted audio should go "
                          "(default: beside the MP3 it came from)")
    run.add_argument("--jobs", type=int, help="parallel conversions")
    run.add_argument("--reencode", action="store_true",
                     help="re-encode to AAC instead of copying the MP3 stream "
                          "into the M4B losslessly")
    run.add_argument("-d", "--delete-mp3", dest="delete_mp3", action="store_true",
                     help="DESTRUCTIVE: delete each MP3 once its M4B is verified. "
                          "Asks first unless -y is given.")
    run.add_argument("-y", "--yes", action="store_true",
                     help="skip the confirmation prompt")
    run.add_argument("--force", action="store_true",
                     help="ignore cached transcripts and re-run ASR")
    run.add_argument("-n", "--dry-run", action="store_true",
                     help="plan only: convert nothing, delete nothing, write "
                          "nothing")
    run.add_argument("--report", help="write the run report to this file")
    run.add_argument("--json", help="write a machine-readable report here")
    run.add_argument("-v", "--verbose", action="store_true")
    run.set_defaults(func=cmd_run, refine=True, convert=True)

    sen = sub.add_parser("sentences", parents=[common],
                         help="print the reference text as it will be cued")
    sen.set_defaults(func=cmd_sentences)

    pr = sub.add_parser("probe", parents=[common],
                        help="show what would be used, without doing work")
    pr.set_defaults(func=cmd_probe)

    cv = sub.add_parser("convert", parents=[common],
                        help="convert MP3 audio to M4B and stop")
    cv.add_argument("--convert-to")
    cv.add_argument("--jobs", type=int)
    cv.add_argument("--force", action="store_true")
    cv.add_argument("--reencode", action="store_true",
                    help="re-encode to AAC instead of copying losslessly")
    cv.add_argument("-d", "--delete-mp3", dest="delete_mp3", action="store_true",
                    help="DESTRUCTIVE: delete each MP3 once its M4B is verified")
    cv.add_argument("-y", "--yes", action="store_true",
                    help="skip the confirmation prompt")
    cv.set_defaults(func=cmd_convert)

    ln = sub.add_parser("lint", help="check SRTs against the app's parser contract")
    ln.add_argument("paths", nargs="+")
    ln.set_defaults(func=cmd_lint)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if _colour_enabled():
        C.enable()

    parser = build_parser()
    # A bare directory (or any option) means `run`.
    known = {"run", "sentences", "probe", "lint", "convert"}
    if argv and argv[0] not in known and not argv[0] in ("-h", "--help", "--version"):
        argv.insert(0, "run")
    if not argv:
        parser.print_help()
        return 0

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    if args.func is cmd_run:
        ensure_library_path()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        log("interrupted")
        return 130
