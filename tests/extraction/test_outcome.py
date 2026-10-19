from datetime import datetime, timezone

from knowledge_forge.extraction.outcome import build_extraction_outcome
from knowledge_forge.extraction.schemas import ExtractionItem, ExtractionResult
from knowledge_forge.models.chunk import Chunk, HierarchyLevel
from knowledge_forge.models.document import Document, DocumentMetadata, TextBlock
from knowledge_forge.models.knowledge_object import KnowledgeObjectType, validate_content
from knowledge_forge.models.source import Source, SourceRef, SourceType

_SOURCE = Source(
    source_id="s1",
    source_type=SourceType.SLACK,
    uri="slack://general/1",
    content_hash="h",
    collected_at=datetime.now(timezone.utc),
)
_DOCUMENT = Document(
    document_id="d1",
    source=SourceRef.from_source(_SOURCE),
    elements=[TextBlock(element_id="e1", text="x")],
    metadata=DocumentMetadata(title="T"),
)
_CHUNK = Chunk(
    chunk_id="c1",
    document_id="d1",
    source_id="s1",
    hierarchy_level=HierarchyLevel.PARAGRAPH,
    sequence_index=0,
    text="x",
    token_count=1,
)


def _outcome(*items: ExtractionItem):
    result = ExtractionResult(summary="s", items=list(items), document_type="slack")
    return build_extraction_outcome(result, [_CHUNK], _DOCUMENT, _SOURCE, model_used="m")


def test_typed_attributes_map_onto_each_types_content_model():
    outcome = _outcome(
        ExtractionItem(type="decision", statement="배포를 월요일로 미룬다", people=["정민찬"], date="2026-10-19",
                       status="decided", confidence=0.9),
        ExtractionItem(type="action_item", statement="QA 마무리", people=["김철수"], date="금요일", status="open"),
        ExtractionItem(type="event", statement="배포 회의", people=["정민찬", "김철수"], date="2026-10-18"),
        ExtractionItem(type="fact", statement="QA는 2주 걸린다", people=["무시됨"]),
    )
    by_type = {ko.type: ko for ko in outcome.objects}

    assert by_type[KnowledgeObjectType.DECISION].content == {
        "statement": "배포를 월요일로 미룬다", "decided_by": ["정민찬"], "decided_at": "2026-10-19", "status": "decided",
    }
    assert by_type[KnowledgeObjectType.ACTION_ITEM].content == {
        "statement": "QA 마무리", "assignees": ["김철수"], "due": "금요일", "status": "open",
    }
    assert by_type[KnowledgeObjectType.EVENT].content == {
        "statement": "배포 회의", "occurred_at": "2026-10-18", "participants": ["정민찬", "김철수"],
    }
    assert by_type[KnowledgeObjectType.FACT].content == {"statement": "QA는 2주 걸린다"}
    for ko in outcome.objects:  # what outcome builds always passes the storage-time check
        assert validate_content(ko.type, ko.content) == ko.content


def test_status_from_the_other_types_vocabulary_is_dropped_not_stored():
    (ko,) = _outcome(ExtractionItem(type="decision", statement="s", status="done")).objects
    assert ko.content["status"] is None


def test_item_confidence_is_kept_and_out_of_range_values_are_discarded():
    kept, out_of_range, missing = _outcome(
        ExtractionItem(type="fact", statement="a", confidence=0.7),
        ExtractionItem(type="fact", statement="b", confidence=7.0),
        ExtractionItem(type="fact", statement="c"),
    ).objects
    assert (kept.confidence, out_of_range.confidence, missing.confidence) == (0.7, None, None)
