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

**Track numbers must count the book, not the batch.** `convert()` takes a
`positions` map built from every track in the folder; without it, converting one
leftover file of twenty-two tags it `1/1`. Tags already written cannot be
revised without re-converting, so a book done in two runs carries two different
totals — `_check_track_numbering()` reports that rather than leaving it to be
found on the phone.

**The conversion is a lossless remux, and `-f mp4` is what makes it possible.**
A `.m4b` extension selects ffmpeg's *ipod* muxer, which refuses MP3 outright —
"Could not find tag for codec mp3" — so without `-f mp4` the remux silently
becomes a failed conversion and falls through to the AAC path. Verified on the
phone: MP3-in-MP4 plays and seeks precisely in shiroikuma-jisho. Bit-identity is
covered by a test that decodes both and compares, not by assertion.

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

**A stale SRT is worse than a missing one.** When a file yields no cues,
`write_for_files` must not simply skip it: an SRT from an earlier run would stay
on disk and keep pairing with that audio, carrying timings for a file that no
longer exists. It is moved aside to a timestamped `.bak` — not deleted, in case
it was hand-edited.

**`--dry-run` covers conversion and deletion too.** Conversion runs at stage 1,
long before the write step, so an early version re-encoded whole audiobooks —
and with `-d`, deleted the originals — while promising to write nothing. Dry
runs now skip conversion and align against the MP3s, saying so.

**`metadata.py` was derived from the library, not invented.** The rules come
from scanning all 127 books and 4,376 files in `~/〇/[197] オーディオブック`: only
82 books have a usable track title in their tags and 8 more in their filenames.
Two findings shaped it. Chapter markers must be **kept** — they name a real part
of the work, and dropping them cost 30 books their titles. And the language
bracket codes are used consistently on ebook files but appear on only 3 of 127
audiobook folders, so language is a bonus, never a source to depend on.

`demojibake()` guards itself by requiring the repaired string to come out ≥80 %
Cyrillic; without that it would mangle `Anéantir`, `Zoë Schiffer` and `Sněženka`.
Keep that check if you touch it, and keep the tests that assert those exact
strings survive.

**Language detection reads the name up to ` -- `, and nothing after it.**
Everything after the separator is 白い熊's tagging — bracket codes, the year, and
genre words written in Japanese (小説, 哲学, ルポルタージュ), which is why the
whole name cannot be used: a German book's folder ends in kanji. Everything
before it is the title and author as written, and carries subtitles and series
text that the `book`/`author` split discards. `before_tags()` does this for both
directory and file names.

Four traps, each with a test:
- Shared diacritics prove nothing — `á` in "Lázár" is Hungarian, not Czech.
- Publisher boilerplate is not evidence — "Opening Credits" and "Chapter" appear
  in German and Japanese audiobook tags.
- An initial is not a word — "Timothy **W.** Ryback" scored Polish and tied.
- Kana ranges include CJK *punctuation* — a German filename mangled into
  `Erw・ungen` carries U+30FB, which declared Der Zauberberg Japanese. The
  script test matches kana letters only.

When the rules find nothing, `dictionaries.py` looks the title's words up in
the system's hunspell dictionaries. That is what reaches «Beyond Order»,
«Greenlights», «Nineteen eighty-four» — plainly English, but holding no
diacritic, no distinctive script and no function word. Three details matter:
only the title and author are looked up (track names are numbers and
boilerplate), words must be four letters or more (three-letter tokens matched
everywhere — "MMA" scored Polish 1.00), and the candidate languages are
restricted to en/de/pl/cs/fr because with every installed dictionary in play
Dutch and Spanish score highly on English titles and turn a clear answer into a
tie. The German and Polish dictionaries are ISO-8859, not UTF-8.

Dictionaries are entirely optional; without them the tool falls back to its own
word lists. With them, 124 of 127 books resolve.

**`und` cannot be removed, only replaced.** MP4's `mdhd` box carries a
mandatory 16-bit packed language field; a file with no language reads `und`
(0x55c4), and so do 白い熊's untouched publisher M4Bs. The answer is therefore
never to omit the tag but to know the language — the EPUB states it, `-l` states
it, `BookInfo.with_language()` fills it in — and to say plainly when it is
genuinely unknown.

MP4 keeps `language` on the **stream**, not the container. A format-level
`-metadata language=…` is accepted by ffmpeg and silently dropped, leaving the
track marked `und`.

**`plan.py` classifies, it does not touch anything.** `inspect()` deliberately
lists rather than probes — surveying the 127-book library would otherwise cost
one ffprobe per file across 4,376 of them; as it stands the survey is instant.
The full `discover()` runs only when a book is actually processed.

**A folder of SRTs is resynced, not re-aligned.** The remux is lossless — the
decoded audio is bit-identical and only the origin moves, by the encoder delay
(25 ms measured on Lázár) — so there is no drift to rediscover and transcribing
hundreds of hours would produce nothing. `_resync_from_srt()` reads the existing
cues, snaps them to the M4B's own pauses (absolute, so the offset corrects
itself) and rewrites. `--realign` runs the full pipeline with the cues as the
reference text, for an SRT that is actually wrong rather than merely moved.
This is the difference between a one-hour library run and an eight-hour one.

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
~/venv.shiroikuma-jisho-subtitles/bin/python -m pytest -q
```

`bin/shiroikuma-jisho-subtitles` carries the full `-h`. Two tests enforce that
every subcommand and every option appears in it, so a new flag fails the suite
until it is documented — a flag nobody can discover may as well not exist.

`-s` (setup/verify) lives in the wrapper rather than in argparse because it has
to run on a machine with no virtualenv — it cannot go through the venv's Python.
Two bash traps to respect there: the file runs under `set -e`, so every bare
conditional used for its side effect needs `|| true`; and `$?` inside an
`if ! cmd; then` branch is the negation's status, not the command's, which once
made a broken environment report itself ready.

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
