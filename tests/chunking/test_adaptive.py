import unicodedata

from knowledge_forge.chunking import AdaptiveChunker
from knowledge_forge.chunking.tokenizer import count_tokens
from knowledge_forge.models import Document, Heading, ListElement, SourceRef, SourceType, TableElement, TextBlock


def make_source_ref() -> SourceRef:
    return SourceRef(source_id="s1", source_type=SourceType.LOCAL_FILE, uri="file:///x")


def test_chunk_text_is_nfc_normalized_even_when_the_source_element_is_nfd():
    """macOS/Google Drive-sourced text can arrive NFD (Korean jamo
    decomposed) - the chunker (not just the DB write, see
    storage/postgres_store.py::save_chunks) normalizes to NFC so every
    in-memory consumer (Knowledge Extraction prompts, chunk embeddings)
    sees the same canonical form the DB row ends up with."""
    nfd_text = unicodedata.normalize("NFD", "프로젝트 관리 문서입니다.")
    assert nfd_text != "프로젝트 관리 문서입니다."  # sanity: decomposition actually changed the bytes
    doc = Document(
        document_id="doc-nfd",
        source=make_source_ref(),
        elements=[TextBlock(element_id="t1", text=nfd_text)],
    )

    chunks = AdaptiveChunker(max_tokens_per_chunk=4000).chunk(doc)

    assert len(chunks) == 1
    assert chunks[0].text == unicodedata.normalize("NFC", nfd_text)
    assert unicodedata.normalize("NFC", chunks[0].text) == chunks[0].text


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


def test_indent_level_is_preserved_as_a_prefix_in_the_emitted_chunk_text():
    doc = Document(
        document_id="doc4",
        source=make_source_ref(),
        elements=[
            TextBlock(element_id="t1", text="top level"),
            TextBlock(element_id="t2", text="nested level", indent_level=1),
        ],
    )
    chunks = AdaptiveChunker(max_tokens_per_chunk=4000).chunk(doc)
    assert len(chunks) == 1
    assert "top level" in chunks[0].text
    assert "  nested level" in chunks[0].text


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


def test_small_sibling_leaves_are_merged_but_a_large_sibling_stays_separate():
    """A title-only heading and a couple of one-word bullets should not
    survive as their own tiny chunks once the section is forced to descend
    to per-element leaves - but a big sibling that would blow the budget if
    absorbed must stay on its own (never merge past budget)."""
    big_text = " ".join(f"word{i}" for i in range(38))  # 76 tokens: fits the 80-token budget alone,
    doc = Document(
        document_id="doc-merge1",
        source=make_source_ref(),
        elements=[
            Heading(element_id="h1", level=1, text="Chapter"),
            TextBlock(element_id="t1", text="hi"),
            TextBlock(element_id="t2", text="bye"),
            TextBlock(element_id="t3", text=big_text),
        ],
    )
    chunks = AdaptiveChunker(max_tokens_per_chunk=100, min_tokens=10).chunk(doc)

    assert len(chunks) == 2
    merged, standalone = chunks
    assert "Chapter" in merged.text and "hi" in merged.text and "bye" in merged.text
    assert merged.element_ids == ["h1", "t1", "t2"]
    assert standalone.element_ids == ["t3"]
    budget = int(100 * 0.8)
    for c in chunks:
        assert count_tokens(c.text) <= budget


def test_table_chunks_never_merge_with_neighboring_non_table_leaves():
    filler_a = " ".join(f"a{i}" for i in range(70))  # 140 tokens: fits the 160-token budget alone
    filler_b = " ".join(f"b{i}" for i in range(70))
    doc = Document(
        document_id="doc-merge2",
        source=make_source_ref(),
        elements=[
            Heading(element_id="h1", level=1, text="Chapter"),
            TextBlock(element_id="t1", text="hi"),
            TableElement(element_id="tb1", rows=[["a", "b"], ["c", "d"]]),
            TextBlock(element_id="t2", text=filler_a),
            TextBlock(element_id="t3", text=filler_b),
        ],
    )
    chunks = AdaptiveChunker(max_tokens_per_chunk=200, min_tokens=10).chunk(doc)

    # Heading+t1 merge (both tiny); the table stands alone on both sides
    # even though its neighbor (heading+t1) is well under min_tokens.
    assert len(chunks) == 4
    assert "a | b" in chunks[1].text and "c | d" in chunks[1].text
    assert chunks[1].element_ids == ["tb1"]


def test_merge_never_crosses_a_chapter_boundary():
    """Two chapters, each individually tiny (well under min_tokens) but
    together over budget, must stay as two separate chunks - Document-level
    children are never merge candidates (see _MERGE_GATE_LEVELS)."""
    # 34 words -> 68 tokens, so each chapter (heading + body) is 72 tokens -
    # under the 80-token budget alone - but the two together are not.
    chapter1_body = " ".join(f"word{i}" for i in range(34))
    chapter2_body = " ".join(f"word{i}" for i in range(34))
    doc = Document(
        document_id="doc-merge3",
        source=make_source_ref(),
        elements=[
            Heading(element_id="h1", level=1, text="Chapter One"),
            TextBlock(element_id="t1", text=chapter1_body),
            Heading(element_id="h2", level=1, text="Chapter Two"),
            TextBlock(element_id="t2", text=chapter2_body),
        ],
    )
    chunks = AdaptiveChunker(max_tokens_per_chunk=100, min_tokens=1000).chunk(doc)

    assert len(chunks) == 2
    assert chunks[0].hierarchy_level.value == "chapter"
    assert "Chapter One" in chunks[0].text
    assert "Chapter Two" in chunks[1].text


def test_default_min_tokens_merges_tiny_slide_like_paragraphs():
    """Regression for the reported pptx/gdrive symptom: a title-only
    paragraph followed by short bullets, using the AdaptiveChunker default
    (no explicit min_tokens), should not each stand alone as ~1-2-token
    chunks."""
    filler = " ".join(f"w{i}" for i in range(2000))  # forces section descent
    doc = Document(
        document_id="doc-merge4",
        source=make_source_ref(),
        elements=[
            Heading(element_id="h1", level=1, text="Slide Title"),
            TextBlock(element_id="t1", text="bullet one"),
            TextBlock(element_id="t2", text="bullet two"),
            TextBlock(element_id="t3", text=filler),
        ],
    )
    chunks = AdaptiveChunker(max_tokens_per_chunk=4000).chunk(doc)

    tiny = [c for c in chunks if count_tokens(c.text) <= 5]
    assert tiny == []
