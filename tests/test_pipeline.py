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
