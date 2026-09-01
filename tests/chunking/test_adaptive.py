from knowledge_forge.chunking import AdaptiveChunker
from knowledge_forge.chunking.tokenizer import count_tokens
from knowledge_forge.models import Document, Heading, ListElement, SourceRef, SourceType, TableElement, TextBlock


def make_source_ref() -> SourceRef:
    return SourceRef(source_id="s1", source_type=SourceType.LOCAL_FILE, uri="file:///x")


def test_small_document_stays_as_a_single_chunk():
    doc = Document(
        document_id="doc1",
        source=make_source_ref(),
        elements=[
            Heading(element_id="h1", level=1, text="Title"),
            TextBlock(element_id="t1", text="A short paragraph."),
        ],
    )
    chunks = AdaptiveChunker(max_tokens_per_chunk=4000).chunk(doc)
    assert len(chunks) == 1
    assert chunks[0].hierarchy_level.value == "document"
    assert "Title" in chunks[0].text
    assert "A short paragraph." in chunks[0].text


def test_every_emitted_chunk_respects_the_token_budget():
    max_tokens = 50
    big_text = " ".join(f"word{i}" for i in range(400)) + "."
    doc = Document(
        document_id="doc2",
        source=make_source_ref(),
        elements=[
            Heading(element_id="h1", level=1, text="Chapter"),
            Heading(element_id="h2", level=2, text="Section"),
            TextBlock(element_id="t1", text=big_text),
            ListElement(element_id="l1", items=["one", "two", "three"]),
            TableElement(element_id="tb1", rows=[["a", "b"], ["c", "d"]]),
        ],
    )
    chunks = AdaptiveChunker(max_tokens_per_chunk=max_tokens).chunk(doc)
    assert len(chunks) > 1
    budget = int(max_tokens * 0.8)
    for c in chunks:
        assert count_tokens(c.text) <= budget


def test_provenance_fields_are_set_and_sequential():
    doc = Document(
        document_id="doc3",
        source=make_source_ref(),
        elements=[TextBlock(element_id="t1", text="hello"), TextBlock(element_id="t2", text="world")],
    )
    chunks = AdaptiveChunker(max_tokens_per_chunk=4000).chunk(doc)
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.document_id == "doc3"
    assert chunk.source_id == "s1"
    assert chunk.sequence_index == 0


def test_forces_sentence_and_line_fallback_on_an_unsplittable_paragraph():
    # A single "sentence" with no internal whitespace at all - even the
    # hard-split fallback must still emit chunks under budget.
    huge_word = "가" * 5000
    doc = Document(
        document_id="doc4",
        source=make_source_ref(),
        elements=[TextBlock(element_id="t1", text=huge_word)],
    )
    max_tokens = 30
    chunks = AdaptiveChunker(max_tokens_per_chunk=max_tokens).chunk(doc)
    assert len(chunks) > 1
    budget = int(max_tokens * 0.8)
    for c in chunks:
        assert count_tokens(c.text) <= budget
    assert "".join(c.text for c in chunks) == huge_word


def test_empty_document_produces_no_chunks():
    doc = Document(document_id="doc5", source=make_source_ref(), elements=[])
    chunks = AdaptiveChunker(max_tokens_per_chunk=4000).chunk(doc)
    assert chunks == []
