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


def test_kss_windows_is_a_lossless_partition_that_never_splits_a_word():
    # Punctuation-free prose is what still reaches kss - windowing it must
    # reproduce the text exactly, stay under the cap, and cut only at spaces.
    text = "서버 장애 원인은 디스크 부족이었고 조치 완료했습니다 " * 100
    windows = _kss_windows(text)
    assert len(windows) > 1
    assert all(len(w) <= _KSS_WINDOW_CHARS for w in windows)
    assert all(w.endswith(" ") for w in windows[:-1])
    assert "".join(windows) == text


def test_kss_windows_leaves_short_text_as_a_single_window():
    text = "짧은 문단." * 10
    assert len(text) <= _KSS_WINDOW_CHARS
    assert _kss_windows(text) == [text]


def test_kss_windows_hard_cuts_a_space_free_run():
    text = "가" * (_KSS_WINDOW_CHARS * 3)
    windows = _kss_windows(text)
    assert all(len(w) <= _KSS_WINDOW_CHARS for w in windows)
    assert "".join(windows) == text


def test_split_sentences_does_not_send_punctuated_prose_to_kss(monkeypatch):
    # Sentence-final punctuation already bounds every piece under
    # _KSS_MIN_CHARS, so kss (quadratic in input length) must not run at all.
    import kss

    def _fail(_text):
        raise AssertionError("kss called on a short piece")

    monkeypatch.setattr(kss, "split_sentences", _fail)
    text = "회의에서 결정된 사항은 다음과 같다. 첫째, 예산은 3분기까지 동결한다. " * 250
    assert len(split_sentences(text)) == 500


def test_split_sentences_stays_fast_on_a_long_punctuation_free_paragraph():
    # Regression test for the quadratic kss blow-up: this ~5k-char paragraph
    # took tens of seconds as one kss call.
    text = "서버 장애 원인은 디스크 부족이었고 조치 완료했습니다 " * 170
    start = time.perf_counter()
    sentences = split_sentences(text)
    elapsed = time.perf_counter() - start
    assert len(sentences) > 1
    assert elapsed < 10, f"split_sentences took {elapsed:.1f}s on ~5k chars - kss input cap may be broken"
