from datetime import datetime, timedelta, timezone

from knowledge_forge.extraction.versioning import resolve_supersedes
from knowledge_forge.models.chunk import HierarchyLevel
from knowledge_forge.models.knowledge_object import DocumentRef, Evidence, KnowledgeObject, KnowledgeObjectType
from knowledge_forge.models.relation import RelationType
from knowledge_forge.models.source import SourceRef, SourceType

_BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _ko(
    ko_id: str,
    ko_type: KnowledgeObjectType,
    summary: str,
    document_id: str = "d1",
    version: int = 1,
    minutes: int = 0,
) -> KnowledgeObject:
    return KnowledgeObject(
        knowledge_object_id=ko_id,
        type=ko_type,
        source=SourceRef(source_id="s1", source_type=SourceType.LOCAL_FILE, uri="file:///a.md"),
        document=DocumentRef(document_id=document_id, title="T"),
        evidence=[Evidence(chunk_id=f"{document_id}-c1", document_id=document_id, hierarchy_level=HierarchyLevel.PARAGRAPH, sequence_index=0)],
        summary=summary,
        content={"statement": summary},
        topics=[],
        document_type="report",
        extracted_at=_BASE_TIME + timedelta(minutes=minutes),
        model_used="claude-opus-5",
        version=version,
        pipeline_version="0.1.0",
        source_hash="h1",
    )


def test_similar_new_object_supersedes_the_prior_version():
    prior = _ko("ko-old", KnowledgeObjectType.DECISION, "The Board approved the merger.", document_id="d1", minutes=0)
    new = _ko("ko-new", KnowledgeObjectType.DECISION, "The Board approved the merger (final).", document_id="d2", minutes=10)

    relations = resolve_supersedes([new], [prior], threshold=0.6)

    assert new.supersedes == "ko-old"
    assert new.version == prior.version + 1
    assert len(relations) == 1
    assert relations[0].type == RelationType.SUPERSEDES
    assert relations[0].subject_id == "ko-new"
    assert relations[0].object_id == "ko-old"


def test_dissimilar_new_object_does_not_supersede_anything():
    prior = _ko("ko-old", KnowledgeObjectType.DECISION, "The Board approved the merger.", document_id="d1")
    new = _ko("ko-new", KnowledgeObjectType.ISSUE, "Regulatory approval is still pending.", document_id="d2")

    relations = resolve_supersedes([new], [prior], threshold=0.8)

    assert new.supersedes is None
    assert new.version == 1
    assert relations == []


def test_each_prior_object_is_claimed_by_at_most_one_new_object():
    prior = _ko("ko-old", KnowledgeObjectType.FACT, "Acme signed the agreement.", document_id="d1")
    new_a = _ko("ko-new-a", KnowledgeObjectType.FACT, "Acme signed the agreement.", document_id="d2")
    new_b = _ko("ko-new-b", KnowledgeObjectType.FACT, "Acme signed the agreement.", document_id="d2")

    relations = resolve_supersedes([new_a, new_b], [prior], threshold=0.9)

    superseded = [r for r in relations if r.type == RelationType.SUPERSEDES]
    assert len(superseded) == 1
    claimant = superseded[0].subject_id
    assert claimant in {"ko-new-a", "ko-new-b"}
    other = new_b if claimant == "ko-new-a" else new_a
    assert other.supersedes is None


def test_no_prior_objects_is_a_no_op():
    new = _ko("ko-new", KnowledgeObjectType.FACT, "Acme signed the agreement.")
    assert resolve_supersedes([new], [], threshold=0.8) == []
    assert new.supersedes is None
