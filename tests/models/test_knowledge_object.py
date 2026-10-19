from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from knowledge_forge.models import (
    Document,
    DocumentRef,
    Evidence,
    Heading,
    HierarchyLevel,
    KnowledgeObject,
    KnowledgeObjectType,
    Relation,
    RelationType,
    SourceRef,
    SourceType,
    TableElement,
    TextBlock,
)
from knowledge_forge.models.knowledge_object import validate_content


def _sample_ko(**overrides) -> KnowledgeObject:
    fields = dict(
        knowledge_object_id="ko1",
        type=KnowledgeObjectType.DECISION,
        source=SourceRef(source_id="s1", source_type=SourceType.LOCAL_FILE, uri="file:///a.md"),
        document=DocumentRef(document_id="d1", title="A Doc"),
        evidence=[Evidence(chunk_id="c1", document_id="d1", hierarchy_level=HierarchyLevel.SECTION, sequence_index=0)],
        summary="a summary",
        content={"statement": "Deploy Monday 10AM"},
        topics=["business"],
        document_type="report",
        extracted_at=datetime.now(timezone.utc),
        model_used="claude-opus-5",
        pipeline_version="0.1.0",
        source_hash="deadbeef",
    )
    fields.update(overrides)
    return KnowledgeObject(**fields)


def test_knowledge_object_round_trips_through_json():
    ko = _sample_ko()

    dumped = ko.model_dump_json()
    restored = KnowledgeObject.model_validate_json(dumped)

    assert restored == ko
    # provenance chain: source_id -> document_id -> evidence[].chunk_id -> knowledge_object_id
    assert restored.source.source_id == "s1"
    assert restored.document.document_id == "d1"
    assert restored.evidence[0].chunk_id == "c1"
    assert restored.type == KnowledgeObjectType.DECISION


def test_knowledge_object_evidence_supports_many_chunks():
    """A KnowledgeObject can be grounded in more than one chunk - see
    models/knowledge_object.py's design note (cardinality is many-to-many,
    not the old single ChunkRef)."""
    ko = _sample_ko(
        evidence=[
            Evidence(chunk_id="c1", document_id="d1", hierarchy_level=HierarchyLevel.SECTION, sequence_index=0),
            Evidence(chunk_id="c2", document_id="d1", hierarchy_level=HierarchyLevel.SECTION, sequence_index=1),
        ]
    )
    assert [e.chunk_id for e in ko.evidence] == ["c1", "c2"]


def test_knowledge_object_versioning_fields_default_and_supersede():
    ko = _sample_ko()
    assert ko.version == 1
    assert ko.supersedes is None

    reextracted = _sample_ko(knowledge_object_id="ko2", version=2, supersedes="ko1")
    assert reextracted.version == 2
    assert reextracted.supersedes == "ko1"


def test_relation_links_two_knowledge_object_ids():
    rel = Relation(
        relation_id="rel1",
        type=RelationType.CAUSED_BY,
        subject_id="ko1",
        object_id="ko2",
        label="caused by",
        confidence=0.9,
        evidence=["c1"],
        created_at=datetime.now(timezone.utc),
    )
    restored = Relation.model_validate_json(rel.model_dump_json())
    assert restored == rel
    assert restored.subject_id == "ko1"
    assert restored.object_id == "ko2"


def test_validate_content_fills_typed_defaults_for_decision_action_item_event_and_entity():
    assert validate_content(KnowledgeObjectType.DECISION, {"statement": "s"}) == {
        "statement": "s", "decided_by": [], "decided_at": None, "status": None,
    }
    assert validate_content(KnowledgeObjectType.ACTION_ITEM, {"statement": "s", "assignees": ["정민찬"], "status": "open"}) == {
        "statement": "s", "assignees": ["정민찬"], "due": None, "status": "open",
    }
    assert validate_content(KnowledgeObjectType.EVENT, {"statement": "s", "occurred_at": "2026-10-01"}) == {
        "statement": "s", "occurred_at": "2026-10-01", "participants": [],
    }
    assert validate_content(KnowledgeObjectType.ENTITY, {"name": "Acme", "entity_type": "ORG"}) == {
        "name": "Acme", "entity_type": "ORG", "description": None,
    }


@pytest.mark.parametrize(
    "type_, content",
    [
        (KnowledgeObjectType.DECISION, {"statement": "s", "status": "done"}),  # action_item vocabulary
        (KnowledgeObjectType.ACTION_ITEM, {"assignees": ["a"]}),  # no statement
        (KnowledgeObjectType.EVENT, {"statement": "s", "where": "x"}),  # unknown key
        (KnowledgeObjectType.ENTITY, {"name": "Acme", "type": "ORG"}),  # pre-O2 key
    ],
)
def test_validate_content_rejects_malformed_typed_content(type_, content):
    with pytest.raises(ValidationError):
        validate_content(type_, content)


def test_validate_content_leaves_untyped_kinds_free_form():
    content = {"statement": "s", "anything": 1}
    assert validate_content(KnowledgeObjectType.FACT, content) is content


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
