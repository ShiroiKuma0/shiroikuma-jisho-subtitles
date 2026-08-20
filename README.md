# shiroikuma-jisho-subtitles

Turn an EPUB and its audiobook into **one-sentence-per-cue SRT files** for
[shiroikuma-jisho](https://github.com/shiroikuma/shiroikuma-jisho).

```
shiroikuma-jisho-subtitles ~/books/Lázár
```

One command. Whatever EPUB, whatever audio — one file or a hundred and eleven —
SRTs land beside each audio file, named to match, and the app picks them up by
itself.

## What it is for

In shiroikuma-jisho you open a foreign-language EPUB, a translation EPUB, an
audiobook and an SRT. The player speaks one subtitle and **stops**, so you can
read the line, look words up in Jisho, and check the translation before moving
on. That only works if every subtitle is exactly one whole sentence, timed
tightly enough that the stop lands in the narrator's pause.

Producing those SRTs used to mean merging audio by hand, deleting front matter
by hand, transcribing, and then splitting the *transcript* into sentences with
abbreviation heuristics. This does the whole thing unattended, and the subtitle
text is the **book's own**, not the transcriber's.

## What it handles without being told

- **Many audio files.** 111 MP3s whose boundaries fall wherever the publisher
  decided, unrelated to the book's chapters. No merging, no re-encoding.
- **Text nobody reads.** Cover, half-title, blurb, author vita, imprint,
  publisher notes, dedication, printed section numbers, the newsletter page at
  the end. Never listed anywhere — it simply matches no audio and is dropped.
- **Audio nobody wrote down.** *«Sie hören Lázár … ein Hörbuch des Argon
  Verlags»*, *«Wydawnictwo Znak … czyta Wojciech Stagenalski»*, and the outro
  that repeats it. Dropped the same way.
- **A whole file with no book text in it** — a publisher intro track — is a
  normal outcome, not an error.
- **Tables of contents inside the reading spine**, which otherwise steal the
  match for every spoken chapter title.
- **Japanese ruby**, vertical writing, `.m4b`/`.m4a`/`.ogg`, files whose
  extension lies about their container, and 1883 German orthography.
- **MP3 audio.** The app cannot seek MP3 — its own warning says auto-pause
  "will fire at the wrong sentence boundaries", which defeats the whole point of
  aligning them precisely. MP3 books are converted to M4B **first**, before
  anything else, and the M4B is written beside its MP3 under the same basename.
  Your MP3s are never touched or deleted; the tool ignores them once an M4B
  exists. `-d` deletes them — but only after each M4B is verified to be real
  audio of the same length, so a bad conversion can never take the original with
  it, and it asks first unless `-y` is given. `--keep-mp3` skips conversion.
- **Damaged files.** Audio is decoded through ffmpeg and the result checked
  against the container's own duration, because a decoder that quietly returns
  five per cent of a file still produces a full set of confident-looking SRTs.

## Install

```
python3 -m venv ~/jisho-subs-venv
~/jisho-subs-venv/bin/pip install -e '.[cuda]'
ln -s "$PWD/bin/shiroikuma-jisho-subtitles" ~/0/bin/
```

`shiroikuma-jisho-subtitles -h` documents the whole pipeline and every option.

`ffmpeg` and `ffprobe` must be on `PATH`. A CUDA GPU is optional but worth
having: `large-v3` runs at ~3.9× realtime on 24 CPU cores and ~86× realtime on
an RTX 5090, so a 7¾-hour book is two hours or five minutes.

## Use

```
shiroikuma-jisho-subtitles BOOKDIR                # language read from the EPUB
shiroikuma-jisho-subtitles BOOKDIR -l de          # or state it
shiroikuma-jisho-subtitles BOOKDIR --report r.txt # keep the run report
shiroikuma-jisho-subtitles BOOKDIR -d                # …and delete the MP3s
shiroikuma-jisho-subtitles BOOKDIR --keep-mp3       # do not convert to M4B
shiroikuma-jisho-subtitles BOOKDIR --dry-run      # align, report, write nothing

shiroikuma-jisho-subtitles convert   -d BOOKDIR      # only MP3 → M4B, then stop
shiroikuma-jisho-subtitles sentences -d BOOKDIR      # the text, as it will be cued
shiroikuma-jisho-subtitles probe     -d BOOKDIR      # what would be used
shiroikuma-jisho-subtitles lint      AUDIODIR        # check the app's SRT contract
```

`BOOKDIR` holds the EPUB (or PDF) and the audio, either loose or in one
subdirectory. Transcripts are cached under `~/.cache/jisho-subs`, so every run
after the first is fast; `--force` re-transcribes.

## How it works

1. **Reference text** — spine order, inline-aware so a drop cap
   `<span>A</span>m Rand` stays `Am Rand`; ruby readings removed; contents pages
   detected structurally and dropped. Split into sentences with `pysbd`.
2. **Transcript** — faster-whisper `large-v3`, batched, word timestamps, cached
   per file, primed with proper nouns mined from the book so «Lázár» does not
   come back as *Lhasa*.
3. **Alignment** — the whole book against the whole audiobook in one pass.
   `difflib` finds anchors, RapidFuzz runs an edit-distance path inside the gaps
   between them. Unmatched reference text and unclaimed audio fall out for free.
4. **Pause snapping** — Silero VAD moves each cue end into the following silence
   and rescues starts that Whisper placed on a hallucinated word.
5. **Output** — one SRT per audio file, written to the app's actual parser
   contract (see `jisho_subs/srt.py`), and a report naming everything dropped.

There is deliberately **no chapter matching**. The usual reason for it is that
global alignment is O(n·m) — true of character-level DP, not of token-level
matching. Measured: 70 000 × 69 000 tokens in 0.8 s; the Japanese worst case,
318 000 × 306 000 characters, in 44.9 s using 40 MB.

## Validated on

| Language | Text | Audio | Length | Audio matched |
|---|---|---|---|---|
| German | EPUB | 111 × MP3 | 7:45 | 93.7 % |
| Polish | EPUB | 23 × MP3 | 10:45 | 96.4 % |
| Japanese | EPUB, vertical + ruby | 24 × M4B | 21:19 | 94.4 % |
| German | EPUB, 1883 orthography | 175 × M4A | 13:55 | 85.6 % |
| Russian | EPUB | 31 × MP3 | 5:11 | 93.8 % |

## Licence

MIT.
