import time

from knowledge_forge.chunking.hierarchy import _KSS_WINDOW_CHARS, _kss_windows, render_element_text, split_sentences
from knowledge_forge.models.document import Heading, ListElement, TableElement, TextBlock


def test_render_element_text_prefixes_indented_text_block():
    element = TextBlock(element_id="t1", text="nested line", indent_level=2)
    assert render_element_text(element) == "    nested line"


def test_render_element_text_indents_every_line_of_a_list():
    element = ListElement(element_id="l1", items=["a", "b"], indent_level=1)
    assert render_element_text(element) == "  - a\n  - b"


def test_render_element_text_indents_a_heading():
    element = Heading(element_id="h1", level=2, text="Sub", indent_level=1)
    assert render_element_text(element) == "  ## Sub"


def test_render_element_text_is_unchanged_without_an_indent_level():
    element = TextBlock(element_id="t1", text="flat line")
    assert render_element_text(element) == "flat line"


def test_render_element_text_ignores_indent_level_on_tables():
    element = TableElement(element_id="tb1", rows=[["a", "b"]], indent_level=3)
    assert render_element_text(element) == "a | b"


def test_kss_windows_is_a_lossless_partition_that_never_exceeds_the_cap():
    # A ~10k-char paragraph with regular Korean sentence boundaries - this is
    # the shape that used to make a single kss.split_sentences() call take
    # minutes (see wiki/adaptive-chunking.md). Windowing it must still
    # reproduce the original text exactly and keep every window under cap.
    sentence = "회의에서 결정된 사항은 다음과 같다. 첫째, 예산은 3분기까지 동결한다. "
    text = sentence * 250  # ~10,250 chars
    windows = _kss_windows(text)
    assert len(windows) > 1
    assert all(len(w) <= _KSS_WINDOW_CHARS for w in windows)
    assert "".join(windows) == text


def test_kss_windows_leaves_short_text_as_a_single_window():
    text = "짧은 문단." * 10
    assert len(text) <= _KSS_WINDOW_CHARS
    assert _kss_windows(text) == [text]


def test_kss_windows_hard_cuts_a_boundary_free_run():
    # No newline and no sentence-final punctuation anywhere - kss can't find
    # a boundary either way, so this must still be capped rather than fed to
    # kss whole.
    text = "가" * (_KSS_WINDOW_CHARS * 3)
    windows = _kss_windows(text)
    assert all(len(w) <= _KSS_WINDOW_CHARS for w in windows)
    assert "".join(windows) == text


def test_split_sentences_stays_fast_on_a_long_paragraph_with_no_newlines():
    # Regression test for the quadratic kss blow-up: a single long,
    # newline-free paragraph must not make split_sentences take anywhere
    # close to the tens of seconds (let alone minutes) it used to before
    # kss's input was capped via _kss_windows().
    sentence = "회의에서 결정된 사항은 다음과 같다. 첫째, 예산은 3분기까지 동결한다. "
    text = sentence * 250  # ~10,250 chars, no newlines
    start = time.perf_counter()
    sentences = split_sentences(text)
    elapsed = time.perf_counter() - start
    assert len(sentences) > 1
    assert elapsed < 10, f"split_sentences took {elapsed:.1f}s on ~10k chars - kss input cap may be broken"
