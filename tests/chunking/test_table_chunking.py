from knowledge_forge.chunking import AdaptiveChunker
from knowledge_forge.chunking.tokenizer import count_tokens
from knowledge_forge.models import Document, SourceRef, SourceType, TableElement


def make_source_ref() -> SourceRef:
    return SourceRef(source_id="s1", source_type=SourceType.LOCAL_FILE, uri="file:///sheet.xlsx")


def _big_table(n_rows: int) -> TableElement:
    header = ["id", "name", "amount"]
    rows = [header] + [[str(i), f"item-{i}", str(i * 10)] for i in range(n_rows)]
    return TableElement(element_id="tb1", rows=rows, caption="Sheet1")


def test_large_table_splits_by_row_and_keeps_header_in_every_chunk():
    doc = Document(document_id="doc1", source=make_source_ref(), elements=[_big_table(500)])
    max_tokens = 100
    chunks = AdaptiveChunker(max_tokens_per_chunk=max_tokens).chunk(doc)

    assert len(chunks) > 1
    budget = int(max_tokens * 0.8)
    header_line = "id | name | amount"
    for c in chunks:
        assert count_tokens(c.text) <= budget
        assert header_line in c.text
        assert "Sheet1" in c.text  # caption preserved in every fragment

    # every data row must show up in exactly one chunk - nothing lost, nothing
    # duplicated. Match the exact rendered row line, not a bare substring:
    # "item-1" is itself a substring of "item-10", "item-11", ... "item-199".
    for i in range(500):
        row_line = f"{i} | item-{i} | {i * 10}"
        occurrences = sum(1 for c in chunks if row_line in c.text)
        assert occurrences == 1, f"row {row_line!r} appeared {occurrences} times"


def test_small_table_stays_as_a_single_chunk():
    doc = Document(document_id="doc2", source=make_source_ref(), elements=[_big_table(2)])
    chunks = AdaptiveChunker(max_tokens_per_chunk=4000).chunk(doc)
    assert len(chunks) == 1
    assert "item-0" in chunks[0].text and "item-1" in chunks[0].text


def test_single_oversized_row_falls_back_gracefully():
    # One row so large that even header + one row exceeds budget - must still
    # terminate with every emitted chunk under budget (via hard-split fallback).
    huge_cell = "x" * 2000
    table = TableElement(element_id="tb2", rows=[["id", "value"], ["1", huge_cell]])
    doc = Document(document_id="doc3", source=make_source_ref(), elements=[table])
    max_tokens = 50
    chunks = AdaptiveChunker(max_tokens_per_chunk=max_tokens).chunk(doc)

    assert len(chunks) >= 1
    budget = int(max_tokens * 0.8)
    for c in chunks:
        assert count_tokens(c.text) <= budget
