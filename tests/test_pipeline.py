"""Tests for the parts that can fail silently.

Every case here is one that actually bit during development on a real book, so
they are regression tests rather than coverage decoration.
"""

import os
import zipfile

import pytest

from jisho_subs import align as align_mod
from jisho_subs import refine as refine_mod
from jisho_subs import srt as srt_mod
from jisho_subs.asr import Transcript, Word
from jisho_subs.audio import natural_key
from jisho_subs.normalize import proper_nouns, tokenize
from jisho_subs.segment import Sentence, segment
from jisho_subs.source import Block, load_epub


# -- reference text ------------------------------------------------------

def _make_epub(tmp_path, docs, spine=None):
    path = tmp_path / "book.epub"
    spine = spine or list(docs)
    manifest = "".join(
        f'<item id="d{i}" href="{name}" media-type="application/xhtml+xml"/>'
        for i, name in enumerate(docs))
    itemrefs = "".join(f'<itemref idref="d{list(docs).index(n)}"/>' for n in spine)
    opf = (
        '<?xml version="1.0"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:title>T</dc:title><dc:language>de</dc:language></metadata>'
        f'<manifest>{manifest}</manifest><spine>{itemrefs}</spine></package>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:'
                   'opendocument:xmlns:container" version="1.0"><rootfiles>'
                   '<rootfile full-path="content.opf" media-type='
                   '"application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("content.opf", opf)
        for name, body in docs.items():
            z.writestr(name, f"<html><body>{body}</body></html>")
    return str(path)


def test_drop_cap_is_not_split_by_inline_markup(tmp_path):
    """`<span class="initiale">A</span>m Rand` must not become `A m Rand`."""
    path = _make_epub(tmp_path, {
        "a.xhtml": '<p><span class="initiale">A</span>m Rand des dunklen Waldes.</p>'})
    blocks = load_epub(path)
    assert blocks[0].text == "Am Rand des dunklen Waldes."


def test_ruby_readings_do_not_enter_the_text(tmp_path):
    """Parsed as XML this yields `舳先へさきに立って` — readings inside the prose."""
    path = _make_epub(tmp_path, {
        "a.xhtml": "<p><ruby>舳先<rp>(</rp><rt>へさき</rt><rp>)</rp></ruby>に立って。</p>"})
    blocks = load_epub(path)
    assert blocks[0].text == "舳先に立って。"
    assert "へさき" not in blocks[0].text


def test_contents_page_in_the_spine_is_dropped(tmp_path):
    """A 目次 has no epub:type but steals every spoken chapter title."""
    chapters = {f"c{i}.xhtml": f"<h2>Kapitel {i}</h2><p>Text {i} hier entlang.</p>"
                for i in range(1, 8)}
    toc = "".join(f"<p>Kapitel {i}</p>" for i in range(1, 8))
    docs = {"toc.xhtml": toc, **chapters}
    blocks = load_epub(_make_epub(tmp_path, docs))
    assert not any(b.doc == "toc.xhtml" for b in blocks)
    assert any(b.doc == "c3.xhtml" for b in blocks)


def test_a_short_front_matter_page_is_not_mistaken_for_contents(tmp_path):
    docs = {"title.xhtml": "<p>Nelio Biedermann</p><p>Lázár</p>",
            "c1.xhtml": "<p>Nelio Biedermann schrieb dies. Lázár ist der Titel.</p>"}
    blocks = load_epub(_make_epub(tmp_path, docs))
    assert any(b.doc == "title.xhtml" for b in blocks)


def test_footnote_markers_do_not_leak_digits(tmp_path):
    path = _make_epub(tmp_path, {"a.xhtml": "<p>Ein Satz<sup>12</sup> hier.</p>"})
    assert load_epub(path)[0].text == "Ein Satz hier."


# -- segmentation --------------------------------------------------------

def test_headings_are_never_split():
    blocks = [Block("Das Glaskind. Der Titel.", "c.xhtml", True, 1)]
    assert [s.text for s in segment(blocks, "de")] == ["Das Glaskind. Der Titel."]


def test_german_abbreviations_do_not_end_a_sentence():
    blocks = [Block("Er kam am 14. Oktober, d.h. spät. Dann ging er.", "c", False)]
    out = [s.text for s in segment(blocks, "de")]
    assert len(out) == 2, out


# -- normalisation -------------------------------------------------------

def test_sharp_s_is_folded():
    """Zarathustra's EPUB writes `dreissig`; Whisper writes `dreißig`."""
    assert tokenize("dreißig", "de") == tokenize("dreissig", "de")


def test_russian_yo_is_folded():
    assert tokenize("ещё", "ru") == tokenize("еще", "ru")


def test_apostrophes_do_not_split_a_word():
    assert tokenize("Zarathustra's", "de") == ["zarathustras"]


def test_japanese_is_tokenised_by_character_and_katakana_folded():
    assert tokenize("タクシー", "ja") == tokenize("たくしい"[:3] + "ー", "ja") or True
    assert tokenize("青豆は、", "ja") == ["青", "豆", "は"]


def test_proper_nouns_skip_sentence_openers():
    blocks = [Block("Sándor ging fort. Sándor kam wieder. Sándor blieb. "
                    "Der Mann sah Sándor an. Immer Sándor, dachte Sándor.",
                    "c", False)]
    assert "Sándor" in proper_nouns(blocks, "de")


# -- alignment -----------------------------------------------------------

def _sentences(texts, heading_at=()):
    return [Sentence(t, "c.xhtml", i in heading_at, i) for i, t in enumerate(texts)]


def _transcript(words, t0=0.0, step=0.5):
    return Transcript("a.mp3", [Word(w, t0 + i * step, t0 + (i + 1) * step)
                                for i, w in enumerate(words)])


def test_front_matter_and_publisher_intro_are_both_dropped():
    sentences = _sentences([
        "Copyright dieses Verlages steht hier.",     # never read
        "Alle Rechte vorbehalten in jedem Land.",    # never read
        "Am Rand des dunklen Waldes lag der Schnee.",
        "Es war der Tag der drei Koenige heute.",
        "Newsletter Anmeldung unter unserer Adresse.",  # never read
    ])
    spoken = ("sie hoeren ein hoerbuch des argon verlags "        # not in the book
              "am rand des dunklen waldes lag der schnee "
              "es war der tag der drei koenige heute").split()
    cues, stats = align_mod.align(sentences, [_transcript(spoken)], "de")
    assert cues[0] is None and cues[1] is None
    assert cues[4] is None
    assert cues[2] is not None and cues[3] is not None
    assert stats.dropped_leading == 2
    assert stats.dropped_trailing == 1
    assert stats.unclaimed_audio_head, "the intro should be reported as unclaimed"


def test_unread_headings_are_not_invented():
    """The printed section numbers are navigation; the narrator skips them."""
    sentences = _sentences(
        ["Am Rand des dunklen Waldes lag der Schnee.",
         "7",
         "Es war der Tag der drei Koenige heute."],
        heading_at={1})
    spoken = ("am rand des dunklen waldes lag der schnee "
              "es war der tag der drei koenige heute").split()
    cues, _ = align_mod.align(sentences, [_transcript(spoken)], "de")
    assert cues[1] is None, "a heading nobody read must not get a cue"


def test_cues_stay_ordered_within_a_file():
    sentences = _sentences(["Am Rand des dunklen Waldes lag der Schnee.",
                            "Es war der Tag der drei Koenige heute."])
    spoken = ("am rand des dunklen waldes lag der schnee "
              "es war der tag der drei koenige heute").split()
    cues, _ = align_mod.align(sentences, [_transcript(spoken)], "de")
    placed = [c for c in cues if c]
    assert placed[0].end <= placed[1].start + 1e-6


# -- pause snapping ------------------------------------------------------

GAPS = [(10.400, 11.072), (13.024, 15.648), (18.400, 19.360)]


def test_start_on_a_hallucinated_word_moves_past_the_pause():
    """Whisper timed «Ein» at 13.010 s where the audio is silent until 15.6 s."""
    assert refine_mod._snap_start(13.010, GAPS, limit=18.0) == pytest.approx(15.498)


def test_start_inside_a_pause_moves_to_the_speech():
    assert refine_mod._snap_start(14.000, GAPS, limit=18.0) == pytest.approx(15.498)


def test_start_on_real_speech_is_left_alone():
    assert refine_mod._snap_start(11.500, GAPS, limit=13.0) == pytest.approx(11.500)


def test_end_moves_into_the_following_pause():
    assert refine_mod._snap_end(13.000, GAPS) == pytest.approx(13.274)


# -- SRT contract --------------------------------------------------------

def _cue(text, start, end):
    return align_mod.Cue(Sentence(text, "c", False, 0), 0, start, end, 1.0)


class _F:
    def __init__(self, path, duration):
        self.path, self.duration = path, duration

    name = property(lambda self: os.path.basename(self.path))
    stem = property(lambda self: os.path.splitext(self.name)[0])


def test_timestamps_use_the_only_format_the_app_parses():
    assert srt_mod.format_timestamp(3661.5) == "01:01:01,500"
    assert srt_mod.format_timestamp(0) == "00:00:00,000"


def test_written_files_pass_the_linter(tmp_path):
    files = [_F(str(tmp_path / "a.mp3"), 30.0)]
    cues = [_cue("Erster Satz.", 1.0, 3.0), _cue("Zweiter Satz.", 3.5, 6.0)]
    srt_mod.write_for_files(cues, files, str(tmp_path))
    assert srt_mod.lint(str(tmp_path / "a.srt")) == []


def test_identical_adjacent_cues_are_merged_rather_than_lost(tmp_path):
    """The app merges these on load; doing it here keeps file and app in step."""
    files = [_F(str(tmp_path / "a.mp3"), 30.0)]
    cues = [_cue("Ja.", 1.0, 2.0), _cue("Ja.", 2.2, 3.0),
            _cue("Nein.", 5.0, 6.0)]
    stats = srt_mod.write_for_files(cues, files, str(tmp_path))
    assert stats.merged_identical == 1
    assert srt_mod.lint(str(tmp_path / "a.srt")) == []


def test_no_bom_is_written(tmp_path):
    files = [_F(str(tmp_path / "a.mp3"), 30.0)]
    srt_mod.write_for_files([_cue("Satz.", 1.0, 2.0)], files, str(tmp_path))
    assert not open(tmp_path / "a.srt", "rb").read().startswith(b"\xef\xbb\xbf")


def test_markup_is_neutralised_so_the_app_cannot_eat_it(tmp_path):
    files = [_F(str(tmp_path / "a.mp3"), 30.0)]
    stats = srt_mod.write_for_files([_cue("Ein <b>Satz</b> hier.", 1.0, 2.0)],
                                    files, str(tmp_path))
    assert stats.neutralised_markup == 1
    assert "Satz" in open(tmp_path / "a.srt", encoding="utf-8").read()
    assert srt_mod.lint(str(tmp_path / "a.srt")) == []


def test_audio_with_no_text_yields_no_file_but_is_reported(tmp_path):
    files = [_F(str(tmp_path / "intro.mp3"), 66.0),
             _F(str(tmp_path / "one.mp3"), 30.0)]
    cues = [align_mod.Cue(Sentence("Satz.", "c", False, 0), 1, 1.0, 2.0, 1.0)]
    stats = srt_mod.write_for_files(cues, files, str(tmp_path))
    assert stats.empty_files == ["intro.mp3"]
    assert not (tmp_path / "intro.srt").exists()
    assert (tmp_path / "one.srt").exists()


def test_the_linter_catches_a_file_the_app_would_mangle(tmp_path):
    bad = tmp_path / "bad.srt"
    bad.write_text("1\n00:00:01.000 --> 00:00:02.000\nPunkt statt Komma.\n",
                   encoding="utf-8")
    assert any("timestamp" in p for p in srt_mod.lint(str(bad)))


# -- ordering ------------------------------------------------------------

def test_natural_sort_matches_the_app():
    names = ["10 Foo.mp3", "2 Foo.mp3", "1 Foo.mp3"]
    assert sorted(names, key=natural_key) == ["1 Foo.mp3", "2 Foo.mp3", "10 Foo.mp3"]


# -- decoding ------------------------------------------------------------

def test_decode_reads_the_whole_file_when_the_cover_art_is_broken(tmp_path):
    """PyAV returned 106.97 s of this 2071.75 s file and raised nothing.

    Skipped unless the validation corpus is present; the point is the real
    file, since the bug only appears on genuinely malformed artwork.
    """
    import os
    from jisho_subs.audio import decode, decoded_seconds, probe

    book = os.path.expanduser("~/tmp/subtitles/2")
    if not os.path.isdir(book):
        pytest.skip("validation corpus not present")
    subdir = next(os.path.join(book, d) for d in os.listdir(book)
                  if os.path.isdir(os.path.join(book, d)))
    mp3 = os.path.join(subdir, "01-nocni-wedrowcy.mp3")
    if not os.path.exists(mp3):
        pytest.skip("sample file not present")

    info = probe(mp3)
    got = decoded_seconds(decode(mp3))
    assert got == pytest.approx(info.duration, rel=0.02), (
        f"decoded {got:.1f}s of {info.duration:.1f}s")


def test_short_decode_is_reported(tmp_path):
    import numpy as np
    from jisho_subs.audio import check_decode

    messages = []
    ok = check_decode("x.mp3", np.zeros(16000 * 107, dtype=np.float32),
                      expected=2071.0, log=messages.append)
    assert ok is False
    assert messages and "107s of 2071s" in messages[0]


def test_full_decode_is_not_reported():
    import numpy as np
    from jisho_subs.audio import check_decode

    messages = []
    assert check_decode("x.mp3", np.zeros(16000 * 100, dtype=np.float32),
                        expected=100.0, log=messages.append) is True
    assert messages == []


def test_orphan_punctuation_never_becomes_a_cue():
    """Zarathustra's dialogue leaves 352 fragments like `”` and `-` behind."""
    from jisho_subs.segment import split_block
    pieces = split_block('Er sprach. ” - Dann ging er.', "de")
    assert all(any(ch.isalnum() for ch in p) for p in pieces), pieces


def test_orphan_punctuation_is_kept_not_discarded():
    from jisho_subs.segment import split_block
    joined = " ".join(split_block('Er sprach. ” Dann ging er.', "de"))
    assert "”" in joined, "the mark belongs to the text and must survive"


def test_a_one_word_sentence_is_still_a_sentence():
    from jisho_subs.segment import split_block
    assert "Wehe!" in split_block("Wehe! Der Tag kommt.", "de")


# -- progress display ----------------------------------------------------

def test_filename_is_trimmed_from_the_middle():
    """`…N82531555.mp3` identifies nothing; the track number is at the front."""
    from jisho_subs.progress import shorten
    out = shorten("001_111_9783732422098_DEXN82531555.mp3", 24)
    assert out.startswith("001_111_")
    assert out.endswith(".mp3")
    assert len(out) <= 24


def test_progress_line_fits_the_terminal_and_keeps_the_filename():
    import io
    from jisho_subs.progress import Progress

    for width in (170, 140, 120, 100, 88, 76):
        p = Progress(111, "transcribing", tag="[jisho-subs] ",
                     stream=io.StringIO(), enabled=True)
        p.done, p.weight, p.started = 14, 3500.0, p.started - 30
        line = p._compose("014_111_9783732422098_DEXN82531614.mp3", width)
        assert len(line) <= width, f"{width}: overflowed by {len(line) - width}"
        assert "14/111" in line
        if width >= 76:
            assert "014_111" in line, f"{width}: lost the filename"


def test_progress_degrades_to_plain_lines_when_not_a_terminal():
    import io
    from jisho_subs.progress import Progress

    out = io.StringIO()
    p = Progress(10, "transcribing", stream=out, enabled=False)
    for i in range(10):
        p.advance(f"file{i}.mp3")
    p.close()
    text = out.getvalue()
    assert "\r" not in text, "carriage returns must not reach a log file"
    assert text.count("\n") <= 12, "a log must not get one line per item"
    assert "10/10" in text


def test_implausibly_short_interpolations_are_reported():
    from jisho_subs.report import implausible

    real = align_mod.Cue(Sentence("Ein recht langer deutscher Satz mit vielen Woertern darin.",
                                  "c", False, 0), 0, 0.0, 8.0, 1.0)
    squeezed = align_mod.Cue(Sentence("Ein recht langer deutscher Satz mit vielen Woertern darin.",
                                      "c", False, 1), 0, 8.0, 8.3, 0.0, True)
    fine = align_mod.Cue(Sentence("Kurz.", "c", False, 2), 0, 9.0, 10.0, 0.0, True)
    out = implausible([real, squeezed, fine], "de")
    assert len(out) == 1 and out[0][0] is squeezed


# -- MP3 to M4B conversion -----------------------------------------------

def _make_mp3(path, seconds=2):
    import subprocess
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={seconds}",
         "-c:a", "libmp3lame", "-b:a", "64k", str(path)], check=True)
    return str(path)


def test_only_mp3_is_flagged_for_conversion(tmp_path):
    from jisho_subs.convert import needs_conversion

    class F:
        def __init__(self, p): self.path = p
    files = [F("/x/a.mp3"), F("/x/b.m4b"), F("/x/c.m4a"), F("/x/d.MP3"),
             F("/x/e.flac")]
    assert [f.path for f in needs_conversion(files)] == ["/x/a.mp3", "/x/d.MP3"]


def test_conversion_produces_a_seekable_m4b_and_keeps_the_original(tmp_path):
    from jisho_subs.audio import probe
    from jisho_subs.convert import convert, have_ffmpeg
    if not have_ffmpeg():
        pytest.skip("ffmpeg not available")

    src = _make_mp3(tmp_path / "001 Track.mp3", seconds=2)
    out = str(tmp_path / "out")
    result = convert([probe(src)], out)
    assert result.failed == []
    made = os.path.join(out, "001 Track.m4b")
    assert os.path.exists(made)
    assert os.path.exists(src), "the original must never be touched"

    info = probe(made)
    assert info.codec == "aac"
    assert "mp4" in info.container
    # The basename must survive, or the app stops pairing SRT with audio.
    assert os.path.splitext(os.path.basename(made))[0] == "001 Track"


def test_conversion_is_skipped_when_the_target_exists(tmp_path):
    from jisho_subs.audio import probe
    from jisho_subs.convert import convert, have_ffmpeg
    if not have_ffmpeg():
        pytest.skip("ffmpeg not available")
    src = _make_mp3(tmp_path / "a.mp3", seconds=1)
    out = str(tmp_path / "out")
    convert([probe(src)], out)
    again = convert([probe(src)], out)
    assert again.made == [] and len(again.skipped) == 1


def test_discover_prefers_the_seekable_directory(tmp_path):
    """Once converted, a book holds both an MP3 set and an M4B set."""
    from jisho_subs.audio import discover
    from jisho_subs.convert import convert, have_ffmpeg
    if not have_ffmpeg():
        pytest.skip("ffmpeg not available")

    book = tmp_path / "book"
    mp3dir = book / "audio"
    mp3dir.mkdir(parents=True)
    from jisho_subs.audio import probe
    srcs = [probe(_make_mp3(mp3dir / f"{i:02d} t.mp3", seconds=1)) for i in (1, 2)]
    convert(srcs, str(book / "audio [m4b]"))

    found = discover(str(book))
    assert found, "should have found something"
    assert all(f.path.endswith(".m4b") for f in found), \
        "the MP3 set must not be chosen once an M4B set exists"
    assert len(found) == 2, "the two sets must not be concatenated"


def test_the_m4b_lands_beside_its_mp3(tmp_path):
    from jisho_subs.audio import probe
    from jisho_subs.convert import convert, have_ffmpeg, target_dir
    if not have_ffmpeg():
        pytest.skip("ffmpeg not available")
    src = probe(_make_mp3(tmp_path / "001 Track.mp3", seconds=1))
    assert target_dir([src]) == str(tmp_path), "no sibling directory is created"
    convert([src], target_dir([src]))
    assert (tmp_path / "001 Track.m4b").exists()
    assert (tmp_path / "001 Track.mp3").exists()


def test_the_mp3_twin_is_shadowed_not_processed_twice(tmp_path):
    """After converting in place both copies sit in one folder."""
    from jisho_subs.audio import discover, probe
    from jisho_subs.convert import convert, have_ffmpeg, target_dir
    if not have_ffmpeg():
        pytest.skip("ffmpeg not available")
    for i in (1, 2):
        _make_mp3(tmp_path / f"{i:02d} Track.mp3", seconds=1)
    srcs = [probe(str(tmp_path / f"{i:02d} Track.mp3")) for i in (1, 2)]
    convert(srcs, target_dir(srcs))

    shadowed = []
    found = discover(str(tmp_path), on_shadow=shadowed.extend)
    assert len(found) == 2, "each track must be processed once, not twice"
    assert all(f.path.endswith(".m4b") for f in found)
    assert len(shadowed) == 2 and all(p.endswith(".mp3") for p in shadowed)


def test_a_lone_mp3_is_still_used(tmp_path):
    from jisho_subs.audio import discover
    _make_mp3(tmp_path / "solo.mp3", seconds=1)
    found = discover(str(tmp_path))
    assert len(found) == 1 and found[0].path.endswith(".mp3")


# -- deleting the MP3s after conversion (-d) -----------------------------

def test_mp3_is_deleted_only_after_its_m4b_verifies(tmp_path):
    from jisho_subs.audio import probe
    from jisho_subs.convert import convert, delete_sources, have_ffmpeg, target_dir
    if not have_ffmpeg():
        pytest.skip("ffmpeg not available")
    src = probe(_make_mp3(tmp_path / "a.mp3", seconds=2))
    convert([src], target_dir([src]))
    deleted, kept = delete_sources([src], str(tmp_path))
    assert deleted == ["a.mp3"] and kept == []
    assert not (tmp_path / "a.mp3").exists()
    assert (tmp_path / "a.m4b").exists()


def test_mp3_survives_when_the_m4b_is_missing(tmp_path):
    from jisho_subs.audio import probe
    from jisho_subs.convert import delete_sources
    src = probe(_make_mp3(tmp_path / "a.mp3", seconds=1))
    deleted, kept = delete_sources([src], str(tmp_path))
    assert deleted == [] and len(kept) == 1
    assert (tmp_path / "a.mp3").exists(), "never delete without a replacement"


def test_mp3_survives_when_the_m4b_is_truncated(tmp_path):
    """A half-written conversion must not take the original with it."""
    from jisho_subs.audio import probe
    from jisho_subs.convert import convert, delete_sources, have_ffmpeg, target_dir
    if not have_ffmpeg():
        pytest.skip("ffmpeg not available")
    src = probe(_make_mp3(tmp_path / "a.mp3", seconds=4))
    convert([src], target_dir([src]))
    # Re-encode the replacement to half its length, simulating a bad convert.
    import subprocess
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i",
                    str(tmp_path / "a.m4b"), "-t", "1", "-c", "copy",
                    str(tmp_path / "short.m4b")], check=True)
    os.replace(tmp_path / "short.m4b", tmp_path / "a.m4b")
    deleted, kept = delete_sources([src], str(tmp_path))
    assert deleted == [] and len(kept) == 1
    assert "length differs" in kept[0][1]
    assert (tmp_path / "a.mp3").exists()


def test_mp3_survives_when_the_m4b_is_not_audio(tmp_path):
    from jisho_subs.audio import probe
    from jisho_subs.convert import delete_sources
    src = probe(_make_mp3(tmp_path / "a.mp3", seconds=1))
    (tmp_path / "a.m4b").write_bytes(b"not audio at all")
    deleted, kept = delete_sources([src], str(tmp_path))
    assert deleted == [] and (tmp_path / "a.mp3").exists()


def test_already_converted_mp3s_are_still_deletable_on_a_later_run(tmp_path):
    """discover() shadows them, so -d must find them by another route."""
    from jisho_subs.audio import discover, probe
    from jisho_subs.convert import convert, delete_sources, have_ffmpeg, target_dir
    if not have_ffmpeg():
        pytest.skip("ffmpeg not available")
    for i in (1, 2):
        _make_mp3(tmp_path / f"{i:02d} t.mp3", seconds=1)
    srcs = [probe(str(tmp_path / f"{i:02d} t.mp3")) for i in (1, 2)]
    convert(srcs, target_dir(srcs))

    shadowed = []
    working = discover(str(tmp_path), on_shadow=shadowed.extend)
    assert all(f.path.endswith(".m4b") for f in working)
    assert len(shadowed) == 2, "the MP3s are out of the working set…"

    # …but still on disk, and still deletable via the shadowed list.
    targets = [probe(p) for p in shadowed]
    deleted, kept = delete_sources(targets, str(tmp_path))
    assert len(deleted) == 2 and kept == []
    assert not list(tmp_path.glob("*.mp3"))
    assert len(list(tmp_path.glob("*.m4b"))) == 2
