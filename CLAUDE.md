# shiroikuma-jisho-subtitles — working notes

Produces one-sentence-per-cue SRTs from an EPUB plus its audiobook, for the
sister app **shiroikuma-jisho** (`~/git/shiroikuma-jisho`). Pure Python CLI, no
Android side.

## Things that are load-bearing — do not "simplify" them

**`difflib.SequenceMatcher(..., autojunk=False)`** in `align.py`. The default
`autojunk=True` discards any token occurring in more than 1 % of the sequence,
which on a novel means every function word. Turning it on silently halves the
match rate.

**HTML-mode parsing plus explicit `rt`/`rp` removal** in `source.py`. EPUB
content is XHTML, so `BeautifulSoup(raw, "lxml-xml")` looks like the right call
— it is not. XML mode keeps ruby readings, turning `<ruby>舳先<rt>へさき</rt></ruby>`
into `舳先へさき`. HTML mode drops them, but only as a side effect of libxml2's
HTML5 handling, so the tags are decomposed by hand as well.

**`_drop_duplicate_docs`** in `source.py`. A 目次 inside the reading spine has no
`epub:type` to filter on and steals the match for every spoken chapter title.
The test is structural — most of the document's lines appear elsewhere in the
book — so it needs no metadata and works in any language.

**Everything in `srt.py`.** The app's `flattenSubtitles()` *rewrites* what it
loads: it drops empty cues, merges cues sharing a timestamp, merges adjacent
cues with identical text less than 500 ms apart, and drops the final cue when
`last.end < secondLast.start`. Read the rules table in the module docstring
before changing the writer, and run `jisho-subs lint` after.

**`ß → ss`** in `normalize.py`. Not cosmetic: it is the difference between 85 %
and 94 % on «Also sprach Zarathustra», whose EPUB mixes 1883 word forms with
modernised ß while Whisper writes modern German.

**`decode()` in `audio.py`, and every caller using it.** Never hand a path
straight to faster-whisper: it decodes with PyAV, and PyAV abandons files with
malformed embedded artwork without raising. «Nocni wędrowcy» carries a JPEG
cover labelled as PNG, and PyAV returned 106.97 s of a 2071.75 s file — the run
finished in 53 s for ten and three quarter hours and wrote 22 complete-looking
SRTs covering a twentieth of the book. `check_decode()` exists so the next such
file is a warning on the first file rather than a discovery after a full run;
do not remove it because it "never fires".

**`_reattach_orphans`** in `segment.py`.  Segmenters return the stray closing
quote or dash that dialogue formatting leaves behind; «Also sprach Zarathustra»
produced 352 of them.  Each became a subtitle showing a single punctuation mark,
timed by interpolation because it matched no audio, and the app stopped playback
on it.  Folding them back lifted that book from 91.7 % placed to 96.6 % and cut
interpolated cues from 363 to 66.

**`_snap_start`** in `refine.py`. Whisper emits words that are not there,
typically a few milliseconds *before* a pause rather than inside it, which is
why the test covers a fragment-then-pause case and not just "start is in
silence".

**MP3 conversion is not optional polish.** The app's own dialog
(`_showMp3SeekWarningDialog`) states that with MP3 "auto-pause will fire at the
wrong sentence boundaries" — so precisely-aligned cues are wasted on MP3. The
ffmpeg invocation in `convert.py` is copied from that dialog verbatim; keep the
two in step if the app changes it. Conversion writes the M4B **beside its MP3**, same
basename, and never touches the original — so one folder ends up holding both
copies of every track. `audio.prefer_seekable()` is what stops the tool
processing each track twice; the app has no such filter, so its chapter list
shows both until the MP3s go — which `-d` does, deleting each MP3 only after
`convert.verify_replacement()` confirms its M4B probes as audio of the same
length. That check is the whole safety story for the one irreversible thing this
tool does; do not weaken it to "the file exists". `-d` used to mean `--dir`, so
the prompt defaults to no.

Do not reuse MP3-derived SRTs against the M4B — AAC encoder delay shifts the
duration (238.315 s → 238.277 s on the first Lázár track), so the timings are
regenerated from the M4B itself.

## Architecture note

There is no chapter matching, and adding it would be a regression. Global
alignment is only expensive at character level; at token level the whole book
matches in about a second (measured: 70 000 × 69 000 tokens, 0.8 s; the Japanese
worst case, 318 000 × 306 000 characters, 44.9 s, 40 MB peak). Chapter matching
is exactly what makes subplz fail when audio files and text chapters do not
correspond — which is the normal case.

## Known cost

The whole-book alignment is fast for word languages (~1 s) but takes **several
minutes on Japanese**, far more than the synthetic benchmark suggested. Real
Japanese is dominated by a handful of very frequent characters, and with
`autojunk=False` — which is mandatory — `difflib` builds huge occurrence lists
for them. It is a once-per-book cost, cached afterwards, and the spinner makes
it visible. If it ever needs fixing, run the anchor pass over character 4-grams
rather than single characters: same sequence length, far more distinctive
tokens.

## Validation corpus

`~/tmp/subtitles/{1..5}` — German (111 MP3), Polish (23 MP3), Japanese (24 M4B,
vertical + ruby), German 1883 orthography (175 M4A, first track is a publisher
intro with no book text), Russian (31 MP3). Between them they cover every edge
case the code guards against. Re-check against all five after touching
`source.py`, `normalize.py` or `align.py`.

## Running it

```
shiroikuma-jisho-subtitles ~/tmp/subtitles/1        # ~/0/bin, wraps the venv
~/jisho-subs-venv/bin/python -m pytest -q
```

`bin/shiroikuma-jisho-subtitles` carries the full `-h`; keep it in step with the
argparse options when adding flags.

Transcripts and VAD results cache in `~/.cache/jisho-subs`; runs after the first
skip the GPU work entirely. Use `--force` to re-transcribe.

`cuda.py` re-execs once to put the venv's own cuDNN/cuBLAS on `LD_LIBRARY_PATH`;
without it CTranslate2 dies in a native abort with no traceback. That is the same
failure the neighbouring `~/0/bin/subplz` wrapper exists to prevent — doing it in
Python means the tool cannot be broken by being invoked some other way.

## House rules

Artefacts 白い熊 looks at go in `~/tmp` with a `yyyy-MM-dd_HH-mm-ss` stamp; the
tool's own scratch goes in `.scratch/`. Never delete a generated SRT or an old
build. No `Co-Authored-By` trailers in commits.
