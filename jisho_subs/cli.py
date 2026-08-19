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

    files = discover(audio_root)
    if not files:
        die(f"no audio files under {audio_root}")
    return source, files


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


def cmd_run(args) -> int:
    from .source import load_source
    from .segment import segment
    from .normalize import proper_nouns
    from .asr import Transcriber, default_cache_dir
    from .align import align
    from .audio import format_hms
    from .srt import write_for_files
    from . import report as report_mod

    started = time.time()
    source, files = _resolve_inputs(args)
    lang = _resolve_language(args, source)
    book = os.path.splitext(os.path.basename(source))[0]

    step(f"1/5  reading {os.path.basename(source)}")
    dropped_docs: List[tuple] = []
    blocks = load_source(source, lambda d, n, r: dropped_docs.append((d, n, r)))
    for d, n, r in dropped_docs:
        log(f"  {C.WARN}dropped{C.RESET} {d}  "
            f"({n} blocks, {r:.0%} of it appears elsewhere — a contents page)")
    sentences = segment(blocks, lang)
    log(f"  {len(blocks)} blocks -> {len(sentences)} sentences")

    step(f"2/5  transcribing {len(files)} audio files "
         f"({format_hms(sum(f.duration for f in files))})")
    names = proper_nouns(blocks, lang)
    prompt = ", ".join(names) if names else None
    if prompt:
        log(f"  priming Whisper with {len(names)} names: "
            f"{C.DIM}{prompt[:70]}…{C.RESET}")
    cache_dir = args.cache_dir or default_cache_dir()
    # The model is loaded on first use, so its message must not land on top of
    # the progress line; let the transcriber log through the bar instead.
    bar = Progress(len(files), "transcribing", tag=f"{C.TAG}[jisho-subs]{C.RESET} ")
    tr = Transcriber(model_name=args.model, device=args.device,
                     batch_size=args.batch_size, beam_size=args.beam_size,
                     cache_dir=cache_dir, initial_prompt=prompt,
                     log=lambda m: bar.note(m.strip()))
    transcripts = []
    for f in files:
        bar.note(f.name)
        transcripts.append(tr.transcribe(f, lang, force=args.force))
        # Cached files did no work; counting them would report a throughput
        # that has nothing to do with the machine.
        bar.advance(f.name, weight=0.0 if tr.last_cached else f.duration)
    bar.close(f"{tr.cache_hits} from cache" if tr.cache_hits else "")

    step("3/5  aligning")
    with Spinner("aligning the book against the audio",
                 tag=f"{C.TAG}[jisho-subs]{C.RESET} "):
        cues, stats = align(sentences, transcripts, lang, log=lambda m: log(m))
    if not stats.placed:
        die("nothing matched — is the language right, or the audio a different book?")
    log(f"  placed {stats.placed}/{len(sentences)} sentences, "
        f"{stats.anchors} anchors, "
        f"{100.0 * stats.matched_hyp / max(1, stats.hyp_tokens):.1f}% of audio claimed")

    if args.refine:
        step("4/5  snapping cue ends into the narrator's pauses")
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
    else:
        step("4/5  skipping pause snapping (--no-refine)")

    step("5/5  writing subtitles")
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
        log(f"  {C.OK}{write_stats.files} SRT files, "
            f"{write_stats.cues} cues{C.RESET}")

    text = report_mod.build(book, lang, source, files, sentences, cues, stats,
                            write_stats, dropped_docs)
    print(text)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        log(f"report written to {args.report}")
    if args.json:
        report_mod.write_json(args.json, book, lang, files, cues, stats, write_stats)
        log(f"machine-readable report written to {args.json}")

    log(f"{C.OK}done in {time.time() - started:.0f}s{C.RESET}")
    return 0


# -- argument parsing ----------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jisho-subs",
        description="Turn an EPUB and its audiobook into one-sentence-per-cue "
                    "SRT files for shiroikuma-jisho.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  jisho-subs -d ~/tmp/subtitles/1                 # language read from the EPUB
  jisho-subs -d ~/books/Lazar -l de --report r.txt
  jisho-subs sentences -d ~/books/Lazar           # just the reference text
  jisho-subs lint ~/books/Lazar/audio             # check written SRTs
  jisho-subs probe -d ~/books/Lazar               # what would be used
""")
    p.add_argument("--version", action="version", version=f"jisho-subs {__version__}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-d", "--dir", dest="directory",
                        help="book directory holding the EPUB and the audio")
    common.add_argument("--epub", help="reference EPUB or PDF (overrides -d)")
    common.add_argument("--audio", help="directory of audio files (overrides -d)")
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
    run.add_argument("--force", action="store_true",
                     help="ignore cached transcripts and re-run ASR")
    run.add_argument("--dry-run", action="store_true", help="do not write SRTs")
    run.add_argument("--report", help="write the run report to this file")
    run.add_argument("--json", help="write a machine-readable report here")
    run.add_argument("-v", "--verbose", action="store_true")
    run.set_defaults(func=cmd_run, refine=True)

    sen = sub.add_parser("sentences", parents=[common],
                         help="print the reference text as it will be cued")
    sen.set_defaults(func=cmd_sentences)

    pr = sub.add_parser("probe", parents=[common],
                        help="show what would be used, without doing work")
    pr.set_defaults(func=cmd_probe)

    ln = sub.add_parser("lint", help="check SRTs against the app's parser contract")
    ln.add_argument("paths", nargs="+")
    ln.set_defaults(func=cmd_lint)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if _colour_enabled():
        C.enable()

    parser = build_parser()
    # Bare `jisho-subs -d DIR` means `run`.
    known = {"run", "sentences", "probe", "lint"}
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
