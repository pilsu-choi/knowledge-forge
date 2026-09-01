from datetime import datetime, timedelta, timezone

from knowledge_forge.dedup import merge_duplicates
from knowledge_forge.models.chunk import HierarchyLevel
from knowledge_forge.models.knowledge_object import DocumentRef, Evidence, KnowledgeObject, KnowledgeObjectType
from knowledge_forge.models.relation import Relation, RelationType
from knowledge_forge.models.source import SourceRef, SourceType

_BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _ko(ko_id: str, ko_type: KnowledgeObjectType, summary: str, chunk_id: str, minutes: int = 0, topics=None) -> KnowledgeObject:
    return KnowledgeObject(
        knowledge_object_id=ko_id,
        type=ko_type,
        source=SourceRef(source_id="s1", source_type=SourceType.LOCAL_FILE, uri="file:///a.md"),
        document=DocumentRef(document_id="d1", title="T"),
        evidence=[Evidence(chunk_id=chunk_id, document_id="d1", hierarchy_level=HierarchyLevel.PARAGRAPH, sequence_index=0)],
        summary=summary,
        content={"statement": summary},
        topics=topics or [],
        document_type="report",
        extracted_at=_BASE_TIME + timedelta(minutes=minutes),
        model_used="claude-opus-5",
        pipeline_version="0.1.0",
        source_hash="h1",
    )


def test_near_identical_same_type_objects_are_merged_into_the_earliest():
    a = _ko("ko1", KnowledgeObjectType.DECISION, "The Board approved the merger.", "c1", minutes=0, topics=["m&a"])
    b = _ko("ko2", KnowledgeObjectType.DECISION, "The Board approved the merger.", "c2", minutes=1, topics=["governance"])

    result = merge_duplicates([a, b], [], threshold=0.9)

    assert [ko.knowledge_object_id for ko in result.objects] == ["ko1"]
    canonical = result.objects[0]
    assert {e.chunk_id for e in canonical.evidence} == {"c1", "c2"}
    assert set(canonical.topics) == {"m&a", "governance"}

    dup_relations = [r for r in result.relations if r.type == RelationType.DUPLICATE_OF]
    assert len(dup_relations) == 1
    assert dup_relations[0].subject_id == "ko2"
    assert dup_relations[0].object_id == "ko1"


def test_dissimilar_text_is_not_merged():
    a = _ko("ko1", KnowledgeObjectType.FACT, "Acme signed the agreement.", "c1")
    b = _ko("ko2", KnowledgeObjectType.FACT, "Regulatory approval is still pending.", "c2")

    result = merge_duplicates([a, b], [], threshold=0.9)

    assert {ko.knowledge_object_id for ko in result.objects} == {"ko1", "ko2"}
    assert result.relations == []


def test_different_types_are_never_merged_even_with_identical_text():
    a = _ko("ko1", KnowledgeObjectType.FACT, "The Board approved the merger.", "c1")
    b = _ko("ko2", KnowledgeObjectType.DECISION, "The Board approved the merger.", "c2")

    result = merge_duplicates([a, b], [], threshold=0.9)

    assert {ko.knowledge_object_id for ko in result.objects} == {"ko1", "ko2"}


def test_existing_relations_pointing_at_a_merged_away_id_are_rewritten_to_the_canonical_id():
    a = _ko("ko1", KnowledgeObjectType.DECISION, "The Board approved the merger.", "c1", minutes=0)
    b = _ko("ko2", KnowledgeObjectType.DECISION, "The Board approved the merger.", "c2", minutes=1)
    other = _ko("ko3", KnowledgeObjectType.ISSUE, "Regulatory approval is pending.", "c1")
    mentions = Relation(
        relation_id="rel1",
        type=RelationType.CAUSED_BY,
        subject_id="ko2",
        object_id="ko3",
        evidence=["c2"],
        created_at=_BASE_TIME,
    )

    result = merge_duplicates([a, b, other], [mentions], threshold=0.9)

    rewritten = next(r for r in result.relations if r.relation_id == "rel1")
    assert rewritten.subject_id == "ko1"
    assert rewritten.object_id == "ko3"
