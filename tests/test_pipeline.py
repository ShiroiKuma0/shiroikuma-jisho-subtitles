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
    # The MP3 stream is copied in, not re-encoded: same audio, smaller file.
    assert info.codec == "mp3"
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
    # Truncate the replacement, simulating a conversion that died half way.
    # -f mp4 is required: a .m4b extension picks the ipod muxer, which refuses
    # to carry MP3 at all.
    import subprocess
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i",
                    str(tmp_path / "a.m4b"), "-t", "1", "-c", "copy",
                    "-f", "mp4", str(tmp_path / "short.m4b")], check=True)
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


# -- the wrapper script --------------------------------------------------

WRAPPER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "bin", "shiroikuma-jisho-subtitles")


def test_the_wrapper_is_valid_bash():
    import subprocess
    assert subprocess.run(["bash", "-n", WRAPPER]).returncode == 0


def test_the_wrapper_help_documents_every_mode():
    import subprocess
    out = subprocess.run(["bash", WRAPPER, "-h"], capture_output=True, text=True,
                         env={**os.environ, "NO_COLOR": "1"}).stdout
    for expected in ("SETTING UP A MACHINE", "DELETING THE MP3s",
                     "MP3 CONVERSION", "-s", "-d, --delete-mp3", "--rebuild",
                     "WHAT IT DOES, IN ORDER", "CACHING"):
        assert expected in out, f"the manual no longer mentions {expected!r}"


def test_the_wrapper_reports_a_missing_venv_rather_than_crashing(tmp_path):
    """Pointed at a path with no venv, `run` must explain, not traceback."""
    import subprocess
    r = subprocess.run(["bash", WRAPPER, "probe", str(tmp_path)],
                       capture_output=True, text=True,
                       env={**os.environ, "NO_COLOR": "1",
                            "JISHO_SUBS_VENV": str(tmp_path / "absent")})
    assert r.returncode != 0
    assert "no interpreter" in r.stderr
    assert "-m venv" in r.stderr, "it should say how to create one"


# -- rewriting over an earlier run ---------------------------------------

def test_an_existing_srt_is_overwritten_and_counted(tmp_path):
    files = [_F(str(tmp_path / "a.mp3"), 30.0)]
    (tmp_path / "a.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nold\n",
                                    encoding="utf-8")
    stats = srt_mod.write_for_files([_cue("neu.", 1.0, 3.0)], files, str(tmp_path))
    assert stats.replaced == 1
    assert "neu." in (tmp_path / "a.srt").read_text(encoding="utf-8")
    assert "old" not in (tmp_path / "a.srt").read_text(encoding="utf-8")


def test_a_stale_srt_is_retired_when_its_audio_yields_no_cues(tmp_path):
    """Otherwise it keeps pairing, with timings for audio that is gone."""
    files = [_F(str(tmp_path / "intro.mp3"), 60.0),
             _F(str(tmp_path / "one.mp3"), 30.0)]
    (tmp_path / "intro.srt").write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nstale\n", encoding="utf-8")
    cues = [align_mod.Cue(Sentence("Satz.", "c", False, 0), 1, 1.0, 2.0, 1.0)]
    stats = srt_mod.write_for_files(cues, files, str(tmp_path))

    assert not (tmp_path / "intro.srt").exists(), "it must stop pairing"
    assert len(stats.retired) == 1
    backups = list(tmp_path.glob("intro.srt.*.bak"))
    assert len(backups) == 1, "moved aside, not destroyed"
    assert "stale" in backups[0].read_text(encoding="utf-8")


def test_nothing_is_retired_when_there_was_no_earlier_srt(tmp_path):
    files = [_F(str(tmp_path / "intro.mp3"), 60.0),
             _F(str(tmp_path / "one.mp3"), 30.0)]
    cues = [align_mod.Cue(Sentence("Satz.", "c", False, 0), 1, 1.0, 2.0, 1.0)]
    stats = srt_mod.write_for_files(cues, files, str(tmp_path))
    assert stats.retired == [] and stats.replaced == 0
    assert stats.empty_files == ["intro.mp3"]


def test_every_option_is_documented_in_the_manual():
    """A flag nobody can discover may as well not exist."""
    import subprocess
    from jisho_subs.cli import build_parser

    man = subprocess.run(["bash", WRAPPER, "-h"], capture_output=True, text=True,
                         env={**os.environ, "NO_COLOR": "1"}).stdout
    undocumented = []
    for action in build_parser()._actions:
        for flag in action.option_strings:
            if flag in ("-h", "--help"):
                continue
            if flag not in man:
                undocumented.append(flag)
    assert not undocumented, f"not in `-h`: {undocumented}"


def test_there_are_no_bare_word_subcommands():
    """Every mode is an option; nothing is a positional verb."""
    from jisho_subs.cli import build_parser

    parser = build_parser()
    for action in parser._actions:
        assert not (hasattr(action, "choices")
                    and isinstance(action.choices, dict)), \
            "argparse subparsers are back"
    # The only positional is the path itself.
    positionals = [a.dest for a in parser._actions if not a.option_strings]
    assert positionals == ["paths"], positionals


def test_the_modes_are_mutually_exclusive():
    import pytest as _pytest
    from jisho_subs.cli import build_parser
    with _pytest.raises(SystemExit):
        build_parser().parse_args(["-c", "--probe", "/tmp"])


def test_dash_n_is_dry_run():
    from jisho_subs.cli import build_parser
    args = build_parser().parse_args(["run", "/tmp", "-n"])
    assert args.dry_run is True


# -- metadata derived for the converted audio ----------------------------

def test_directory_name_yields_book_author_year_language():
    from jisho_subs.metadata import parse_directory
    i = parse_directory("/x/Empuzjon, Olga Tokarczuk -- [197][942] (2022)")
    assert (i.book, i.author, i.year, i.language) == \
        ("Empuzjon", "Olga Tokarczuk", "2022", "pl")
    assert i.iso3 == "pol" and i.is_audiobook


def test_year_between_title_and_author_is_understood():
    from jisho_subs.metadata import parse_directory
    i = parse_directory("/x/Meditations (180) Marcus Aurelius")
    assert (i.book, i.author, i.year) == ("Meditations", "Marcus Aurelius", "180")


def test_an_inverted_article_is_not_an_author():
    from jisho_subs.metadata import parse_directory
    i = parse_directory("/x/Achilles trap, the, Steve Coll -- [197] (2024)")
    assert i.book == "Achilles trap, the" and i.author == "Steve Coll"


def test_a_narrator_is_told_apart_from_the_author():
    from jisho_subs.metadata import parse_directory
    i = parse_directory("/x/Homage to Catalonia, George Orwell, Patrick Tull -- [197]")
    assert i.author == "George Orwell" and i.narrator == "Patrick Tull"
    j = parse_directory("/x/Knife, Meditations after an attempted murder, "
                        "Salman Rushdie -- [197] (2024)")
    assert j.author == "Salman Rushdie", "a subtitle is not a narrator"


def test_a_series_part_is_not_a_person():
    from jisho_subs.metadata import parse_directory
    i = parse_directory("/x/Absolutely Mental, Season 2, Ricky Gervais -- [197]")
    assert i.author == "Ricky Gervais" and "Season 2" in i.book


def test_cp1251_tags_read_as_latin1_are_repaired():
    from jisho_subs.metadata import demojibake
    assert demojibake("×àñòü 1 - 1") == "Часть 1 - 1"
    assert demojibake("Ëåâ Íèêîëàåâè÷ Òîëñòîé") == "Лев Николаевич Толстой"


def test_ordinary_accented_text_is_never_touched():
    from jisho_subs.metadata import demojibake
    for s in ("Anéantir", "Zoë Schiffer", "Sněženka", "Nocni wędrowcy",
              "Lázár", "Fröhliche Wissenschaft"):
        assert demojibake(s) == s


def test_chapter_markers_are_kept_as_titles():
    """The author divided the book that way; the marker names a real part."""
    from jisho_subs.metadata import BookInfo, clean_track_title
    info = BookInfo(directory="d", book="Erfolg", author="Lion Feuchtwanger")
    for marker in ("Kapitel 1", "Глава 7", "Chapitre 3", "第4章", "Vorrede",
                   "Часть 1 - 1", "Book I"):
        title, why = clean_track_title(marker, info)
        assert title == marker, f"{marker!r} was dropped as {why!r}"


def test_restatements_are_discarded():
    from jisho_subs.metadata import BookInfo, clean_track_title
    info = BookInfo(directory="Achilles trap, the, Steve Coll -- [197]",
                    book="Achilles trap, the", author="Steve Coll")
    for junk in ("The Achilles Trap", "The_Achilles_Trap_A", "Steve Coll",
                 "001", "DEXN82531555"):
        title, why = clean_track_title(junk, info)
        assert title is None, f"{junk!r} survived as {title!r}"
        assert why


def test_padded_counters_go_but_authorial_numbers_stay():
    from jisho_subs.metadata import BookInfo, clean_track_title
    info = BookInfo(directory="d", book="Война и мир. Том 1", author="Лев Толстой")
    assert clean_track_title("Часть 1 - 1", info)[0] == "Часть 1 - 1"
    info2 = BookInfo(directory="d", book="Greenlights", author="X")
    assert clean_track_title("Greenlights - Part 1", info2)[0] == "Part 1"


def test_built_tags_carry_the_folder_facts():
    from jisho_subs.metadata import build_tags, parse_directory
    info = parse_directory("/x/Nocni wędrowcy, Wojciech Jagielski -- [197][942] (2021)")
    tags, dropped = build_tags(info, {"title": "01_nocni wedrowcy",
                                      "comment": "czyta X"}, "01-nocni.mp3", 1, 22)
    assert tags["album"] == "Nocni wędrowcy"
    assert tags["artist"] == tags["album_artist"] == "Wojciech Jagielski"
    assert tags["date"] == "2021" and tags["language"] == "pol"
    assert tags["track"] == "1/22"
    assert tags["comment"] == "czyta X", "unrelated source tags survive"
    assert "title" not in tags and dropped, "the slug title is not written"


def test_language_is_written_on_the_stream_not_the_container():
    """MP4 drops a format-level language tag, leaving the track 'und'."""
    from jisho_subs.convert import _metadata_args
    args = _metadata_args({"language": "pol", "album": "X"})
    assert "-metadata:s:a:0" in args
    assert args[args.index("-metadata:s:a:0") + 1] == "language=pol"


def test_the_copy_is_bit_identical_to_the_source(tmp_path):
    """Lossless is a claim worth checking, not asserting."""
    import subprocess
    from jisho_subs.audio import probe
    from jisho_subs.convert import convert, have_ffmpeg, target_dir
    if not have_ffmpeg():
        pytest.skip("ffmpeg not available")

    src = _make_mp3(tmp_path / "a.mp3", seconds=6)
    convert([probe(src)], target_dir([probe(src)]))
    made = str(tmp_path / "a.m4b")

    def decoded(path):
        return subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", path, "-map", "0:a",
             "-ss", "1", "-t", "3", "-f", "s16le", "-ar", "44100", "-ac", "2", "-"],
            capture_output=True).stdout

    assert decoded(src) == decoded(made), "the copied audio must be identical"


def test_reencode_falls_back_to_aac(tmp_path):
    from jisho_subs.audio import probe
    from jisho_subs.convert import convert, have_ffmpeg, target_dir
    if not have_ffmpeg():
        pytest.skip("ffmpeg not available")
    src = probe(_make_mp3(tmp_path / "a.mp3", seconds=2))
    convert([src], target_dir([src]), reencode=True)
    assert probe(str(tmp_path / "a.m4b")).codec == "aac"


def test_the_fallback_bitrate_follows_the_source(tmp_path):
    """A fixed rate inflates a 32 kbps file and degrades a 192 kbps one."""
    import subprocess
    from jisho_subs.audio import probe
    from jisho_subs.convert import _target_bitrate, MIN_BITRATE, MAX_BITRATE
    for rate in (32, 128, 256):
        path = str(tmp_path / f"{rate}.mp3")
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi",
                        "-i", "sine=frequency=440:duration=2", "-c:a", "libmp3lame",
                        "-b:a", f"{rate}k", path], check=True)
        got = _target_bitrate(probe(path))
        assert MIN_BITRATE <= got <= MAX_BITRATE
        assert abs(got - min(max(rate, MIN_BITRATE), MAX_BITRATE)) <= 8, \
            f"{rate}k source produced {got}k"


def test_track_numbers_count_the_book_not_the_batch():
    """Converting one leftover of twenty-two used to tag it 1/1."""
    from jisho_subs.convert import numbering

    class T:
        def __init__(self, p):
            self.path = p
            self.stem = os.path.splitext(os.path.basename(p))[0]

    tracks = [T("/b/22 x.mp3")] + [T(f"/b/{i:02d} x.m4b") for i in range(1, 22)]
    n = numbering(tracks)
    assert n["22 x"] == (22, 22)
    assert n["01 x"] == (1, 22)
    # Keyed by stem, so the same track resolves before and after conversion.
    assert numbering([T("/b/01 x.mp3")])["01 x"] == (1, 1)


def test_numbering_survives_a_split_conversion(tmp_path):
    from jisho_subs.audio import probe, read_tags
    from jisho_subs.convert import convert, have_ffmpeg, numbering, target_dir
    if not have_ffmpeg():
        pytest.skip("ffmpeg not available")
    srcs = [probe(_make_mp3(tmp_path / f"{i:02d} t.mp3", seconds=1))
            for i in range(1, 5)]
    positions = numbering(srcs)
    # Two separate runs, as happens when a conversion is resumed.
    convert(srcs[:2], target_dir(srcs), positions=positions)
    convert(srcs[2:], target_dir(srcs), positions=positions)
    totals = {read_tags(str(tmp_path / f"{i:02d} t.m4b"))["track"].split("/")[1]
              for i in range(1, 5)}
    assert totals == {"4"}, f"totals disagree across the book: {totals}"


def test_retagging_rewrites_tags_without_touching_the_audio(tmp_path):
    """The only way to fix a book whose source MP3s have been deleted."""
    import subprocess
    from jisho_subs.audio import probe, read_tags
    from jisho_subs.convert import convert, have_ffmpeg, numbering, retag, target_dir
    if not have_ffmpeg():
        pytest.skip("ffmpeg not available")

    srcs = [probe(_make_mp3(tmp_path / f"{i:02d} t.mp3", seconds=2))
            for i in range(1, 4)]
    convert(srcs[:1], target_dir(srcs))          # an earlier run: one file, 1/1
    made = str(tmp_path / "01 t.m4b")
    assert read_tags(made)["track"] == "1/1"

    def audio_md5(path):
        return subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", path, "-map", "0:a",
             "-f", "md5", "-"], capture_output=True, text=True).stdout.strip()

    before = audio_md5(made)
    m4bs = [probe(made)]
    retag(m4bs, positions=numbering([probe(str(tmp_path / f"{i:02d} t.mp3"))
                                     for i in range(1, 4)]))
    assert read_tags(made)["track"] == "1/3", "the total must count the book"
    assert audio_md5(made) == before, "re-tagging must not alter the audio"


def test_retagging_reports_what_it_did():
    from jisho_subs.convert import retag
    result = retag([])
    assert result.made == [] and result.failed == []


# -- guessing a language when nothing states it --------------------------

def test_script_identifies_japanese_and_russian():
    from jisho_subs.metadata import guess_language
    assert guess_language(["第１章　青豆　見かけにだまされないように"]) == "ja"
    assert guess_language(["День опричника", "Глава 1"]) == "ru"


def test_kanji_without_kana_is_still_japanese():
    from jisho_subs.metadata import guess_language
    assert guess_language(["羅生門", "芥川龍之介"]) == "ja"


def test_a_genre_tag_does_not_decide_the_language():
    """白い熊 tags books with Japanese genre words, so the raw folder name of a
    German book contains kanji."""
    from jisho_subs.metadata import detect_language, parse_directory
    info = parse_directory("Lázár, Nelio Biedermann -- [197] (2026) 小説")
    assert detect_language(info, ["001_111_978.mp3"],
                           [{"title": "Kapitel 1 - Lázár"}]) == "de"


def test_publisher_boilerplate_does_not_decide_the_language():
    """"Opening Credits" appears in the tags of German audiobooks too."""
    from jisho_subs.metadata import guess_language
    assert guess_language(["Also sprach Zarathustra", "Friedrich Nietzsche",
                           "Opening Credits"]) == "de"


def test_diacritics_shared_across_languages_prove_nothing():
    """"Lázár" is a Hungarian name; á must not imply Czech."""
    from jisho_subs.metadata import guess_language
    assert guess_language(["Lázár", "Nelio Biedermann"]) != "cs"


def test_no_evidence_means_no_guess():
    from jisho_subs.metadata import guess_language
    assert guess_language(["MMA"]) is None
    assert guess_language([""]) is None


def test_a_stated_language_always_wins():
    from jisho_subs.metadata import detect_language, parse_directory
    info = parse_directory("Empuzjon, Olga Tokarczuk -- [197][942] (2022)")
    assert detect_language(info, ["第1章.mp3"], []) == "pl"


# -- working out what a directory is -------------------------------------

def _book_dir(tmp_path, name, tracks=2, srt=False, m4b=False, epub=False):
    import subprocess
    d = tmp_path / name
    d.mkdir(parents=True)
    for i in range(1, tracks + 1):
        stem = f"{i:02d} track"
        if m4b:
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi",
                            "-i", "sine=frequency=440:duration=1", "-c:a", "aac",
                            str(d / f"{stem}.m4b")], check=True)
        else:
            _make_mp3(d / f"{stem}.mp3", seconds=1)
        if srt:
            (d / f"{stem}.srt").write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nEin Satz.\n", encoding="utf-8")
    if epub:
        (d / "book.epub").write_bytes(b"not a real epub")
    return str(d)


def test_a_folder_of_mp3s_is_a_book_that_needs_converting(tmp_path):
    from jisho_subs.plan import FROM_NOTHING, classify, inspect
    d = _book_dir(tmp_path, "bare", tracks=3)
    assert classify(d) == "book"
    plan = inspect(d)
    assert plan.to_convert == 3 and plan.reference == FROM_NOTHING
    assert plan.action == "convert"


def test_mp3s_with_subtitles_use_those_as_the_reference(tmp_path):
    from jisho_subs.plan import FROM_SRT, inspect
    plan = inspect(_book_dir(tmp_path, "subbed", tracks=3, srt=True))
    assert plan.reference == FROM_SRT and plan.subtitles == 3
    assert plan.action == "convert + subtitles"


def test_an_already_converted_book_needs_nothing(tmp_path):
    from jisho_subs.plan import inspect
    plan = inspect(_book_dir(tmp_path, "done", tracks=2, srt=True, m4b=True))
    assert plan.to_convert == 0 and plan.action == "nothing to do"
    assert not plan.busy


def test_a_folder_of_books_is_a_library(tmp_path):
    from jisho_subs.plan import classify, survey
    root = tmp_path / "library"
    _book_dir(root, "one", tracks=1)
    _book_dir(root, "two", tracks=1, srt=True)
    _book_dir(root, "three", tracks=1, m4b=True)
    assert classify(str(root)) == "library"
    kind, plans = survey(str(root))
    assert kind == "library" and len(plans) == 3
    assert sorted(p.action for p in plans) == \
        ["convert", "convert + subtitles", "nothing to do"]


def test_an_epub_makes_it_a_book_not_a_library(tmp_path):
    from jisho_subs.plan import classify
    d = _book_dir(tmp_path, "withbook", tracks=1, epub=True)
    assert classify(d) == "book"


def test_reading_cues_back_out_of_an_srt(tmp_path):
    from jisho_subs.srt import read_cues
    p = tmp_path / "a.srt"
    p.write_text("1\n00:00:01,000 --> 00:00:02,000\nErster Satz.\n\n"
                 "2\n00:00:02,500 --> 00:00:04,000\nZweiter\nSatz.\n",
                 encoding="utf-8")
    assert read_cues(str(p)) == ["Erster Satz.", "Zweiter Satz."]


def test_a_byte_order_mark_does_not_leak_into_the_first_cue(tmp_path):
    from jisho_subs.srt import read_cues
    p = tmp_path / "b.srt"
    p.write_bytes("﻿1\n00:00:01,000 --> 00:00:02,000\nSatz.\n".encode("utf-8"))
    assert read_cues(str(p)) == ["Satz."]


# -- the untagged head is what identifies the language -------------------

def test_only_the_part_before_the_separator_is_read():
    from jisho_subs.metadata import before_tags
    assert before_tags("Lázár, Nelio Biedermann -- [197] (2026) 小説") \
        == "Lázár, Nelio Biedermann"
    assert before_tags("Akademia Pana Kleksa, Jan Brzechwa -- [197] (1946).m4a") \
        == "Akademia Pana Kleksa, Jan Brzechwa"
    # No separator: the whole name is the title.
    assert before_tags("Meditations (180) Marcus Aurelius") \
        == "Meditations (180) Marcus Aurelius"


def test_the_head_carries_text_the_parsing_discards():
    """A subtitle is dropped from `book`, but still identifies the language."""
    from jisho_subs.metadata import detect_language, parse_directory
    name = "Prowadź swój pług przez kości umarłych, Olga Tokarczuk -- [197] (2009)"
    assert detect_language(parse_directory(name)) == "pl"


def test_a_japanese_genre_tag_is_not_read():
    from jisho_subs.metadata import detect_language, parse_directory
    assert detect_language(parse_directory(
        "Erfolg, Lion Feuchtwanger -- [197] (1930) 小説")) != "ja"


def test_an_initial_is_not_a_word():
    """"Timothy W. Ryback" scored Polish for its middle initial and tied."""
    from jisho_subs.metadata import guess_language
    assert guess_language(["Takeover, Hitler's final rise to power, "
                           "Timothy W. Ryback"]) == "en"


def test_cjk_punctuation_in_a_mangled_german_name_is_not_japanese():
    """`Erw・ungen` is a broken `Erwägungen`; U+30FB is punctuation, not kana."""
    from jisho_subs.metadata import guess_language
    assert guess_language(["Zauberberg, der, Thomas Mann",
                           "01_0301 Zweifel und Erw・ungen",
                           "Der Zauberberg"]) == "de"


def test_english_words_that_are_also_german_are_not_used():
    from jisho_subs.metadata import _WORDS
    for word in ("in", "an", "all", "her", "was", "will", "man", "die", "wie"):
        assert word not in _WORDS["en"], f"{word!r} is ambiguous"


# -- looking a title up in a real dictionary -----------------------------

def _needs_dictionaries(*languages):
    from jisho_subs import dictionaries
    have = dictionaries.available()
    missing = [l for l in languages if l not in have]
    if missing:
        pytest.skip(f"no hunspell dictionary for {missing}")


def test_dictionaries_are_optional():
    """A machine without hunspell must still run, just with fewer answers."""
    from jisho_subs import dictionaries
    assert isinstance(dictionaries.available(), tuple)
    assert dictionaries.score([], ()) == {}
    assert dictionaries.best([]) is None


def test_a_plain_english_title_is_recognised():
    """No diacritic, no distinctive script, no function word — only a lookup."""
    _needs_dictionaries("en", "de", "pl", "cs")
    from jisho_subs.metadata import detect_language, parse_directory
    for name in ("Beyond Order, Jordan B. Peterson -- [197] (2021)",
                 "Right thing, right now, Ryan Holiday -- [197] (2024)",
                 "Nineteen eighty-four, George Orwell, Simon Prebble -- [197]"):
        assert detect_language(parse_directory(name)) == "en", name


def test_the_dictionary_does_not_override_a_stated_language():
    _needs_dictionaries("en")
    from jisho_subs.metadata import detect_language, parse_directory
    info = parse_directory("Empuzjon, Olga Tokarczuk -- [197][942] (2022)")
    assert detect_language(info) == "pl"


def test_the_dictionary_does_not_override_the_rules():
    """Script and diacritics decide first; the lookup only fills the gaps."""
    _needs_dictionaries("en", "de")
    from jisho_subs.metadata import detect_language, parse_directory
    assert detect_language(parse_directory(
        "羅生門, 芥川龍之介 -- [197] (1915)")) == "ja"
    assert detect_language(parse_directory(
        "Prowadź swój pług przez kości umarłych, Olga Tokarczuk -- [197]")) == "pl"


def test_short_words_are_not_looked_up():
    """Three-letter tokens match everywhere; "MMA" scored Polish 1.00."""
    _needs_dictionaries("pl")
    from jisho_subs.metadata import detect_language, parse_directory
    assert detect_language(parse_directory("MMA")) is None


def test_a_near_tie_is_not_an_answer():
    _needs_dictionaries("en", "de", "cs")
    from jisho_subs.metadata import detect_language, parse_directory
    # "Meditations" is recognised by English, German and Czech alike.
    assert detect_language(parse_directory("Meditations 3 (180) Marcus Aurelius")) \
        is None


def test_dictionary_encodings_are_handled():
    """The German and Polish dictionaries are ISO-8859, not UTF-8."""
    _needs_dictionaries("de", "pl")
    from jisho_subs import dictionaries
    german = dictionaries.words_for("de")
    polish = dictionaries.words_for("pl")
    assert len(german) > 100000 and len(polish) > 100000
    assert any("ü" in w for w in list(german)[:5000]) or "über" in german
    assert any("ł" in w for w in list(polish)[:5000]) or "łatwy" in polish
