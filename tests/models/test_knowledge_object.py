from datetime import datetime, timezone

from knowledge_forge.models import (
    ChunkRef,
    Document,
    DocumentRef,
    Entity,
    Heading,
    HierarchyLevel,
    KnowledgeObject,
    Relation,
    SourceRef,
    SourceType,
    TableElement,
    TextBlock,
)


def test_knowledge_object_round_trips_through_json():
    ko = KnowledgeObject(
        knowledge_object_id="ko1",
        source=SourceRef(source_id="s1", source_type=SourceType.LOCAL_FILE, uri="file:///a.md"),
        document=DocumentRef(document_id="d1", title="A Doc"),
        chunk=ChunkRef(chunk_id="c1", hierarchy_level=HierarchyLevel.SECTION, sequence_index=0),
        summary="a summary",
        facts=["fact one"],
        entities=[Entity(name="Acme", type="ORG")],
        relations=[Relation(subject="Acme", predicate="owns", object="Widget")],
        topics=["business"],
        document_type="report",
        extracted_at=datetime.now(timezone.utc),
        model_used="claude-opus-5",
    )

    dumped = ko.model_dump_json()
    restored = KnowledgeObject.model_validate_json(dumped)

    assert restored == ko
    # PRD section 4's JSON shape: source/document/chunk/summary/facts/entities/relations
    assert restored.source.source_id == "s1"
    assert restored.document.document_id == "d1"
    assert restored.chunk.chunk_id == "c1"


def test_document_discriminates_mixed_element_types():
    doc = Document(
        document_id="d1",
        source=SourceRef(source_id="s1", source_type=SourceType.LOCAL_FILE, uri="file:///a.md"),
        elements=[
            Heading(element_id="h1", level=1, text="Title"),
            TextBlock(element_id="t1", text="body"),
            TableElement(element_id="tb1", rows=[["a", "b"]]),
        ],
    )
    restored = Document.model_validate_json(doc.model_dump_json())
    assert [type(e).__name__ for e in restored.elements] == ["Heading", "TextBlock", "TableElement"]
