from datetime import datetime, timezone
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from knowledge_forge.extraction.claude_extractor import ClaudeExtractor
from knowledge_forge.extraction.schemas import ExtractionEntity, ExtractionItem, ExtractionRelation, ExtractionResult
from knowledge_forge.models.chunk import Chunk, HierarchyLevel
from knowledge_forge.models.document import Document, DocumentMetadata, TextBlock
from knowledge_forge.models.knowledge_object import KnowledgeObjectType
from knowledge_forge.models.relation import RelationType
from knowledge_forge.models.source import Source, SourceRef, SourceType

_REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def _rate_limit_error() -> anthropic.RateLimitError:
    response = httpx2.Response(
        429, request=_REQUEST, json={"error": {"type": "rate_limit_error", "message": "slow down"}}
    )
    return anthropic.RateLimitError("slow down", response=response, body=None)


def _bad_request_error() -> anthropic.BadRequestError:
    response = httpx2.Response(
        400, request=_REQUEST, json={"error": {"type": "invalid_request_error", "message": "bad input"}}
    )
    return anthropic.BadRequestError("bad input", response=response, body=None)


def _sample_result() -> ExtractionResult:
    return ExtractionResult(
        summary="A short summary.",
        entities=[ExtractionEntity(name="Acme Corp", type="ORG"), ExtractionEntity(name="the Board", type="ORG")],
        relations=[ExtractionRelation(subject="Acme Corp", predicate="reports to", object="the Board")],
        items=[
            ExtractionItem(
                type="issue",
                statement="Regulatory approval is still pending.",
                related_entities=["Acme Corp"],
            ),
            ExtractionItem(
                type="decision",
                statement="The Board approved the merger.",
                related_entities=["the Board"],
                caused_by_index=0,
            ),
        ],
        topics=["contracts"],
        document_type="contract",
    )


def _sample_chunk() -> Chunk:
    return Chunk(
        chunk_id="c1",
        document_id="d1",
        source_id="s1",
        hierarchy_level=HierarchyLevel.PARAGRAPH,
        sequence_index=0,
        text="Acme Corp signed the agreement.",
        token_count=6,
        element_ids=["e1"],
    )


def _sample_document() -> Document:
    return Document(
        document_id="d1",
        source=SourceRef(source_id="s1", source_type=SourceType.LOCAL_FILE, uri="file:///contract.txt"),
        elements=[TextBlock(element_id="e1", text="Acme Corp signed the agreement.")],
        metadata=DocumentMetadata(title="Sample Contract"),
    )


def _sample_source() -> Source:
    return Source(
        source_id="s1",
        source_type=SourceType.LOCAL_FILE,
        uri="file:///contract.txt",
        content_hash="deadbeef",
        collected_at=datetime.now(timezone.utc),
    )


class _FakeMessages:
    def __init__(self, side_effects):
        self._side_effects = list(side_effects)
        self.calls = 0

    async def parse(self, **kwargs):
        self.calls += 1
        effect = self._side_effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return SimpleNamespace(parsed_output=effect)


class _FakeClient:
    def __init__(self, side_effects):
        self.messages = _FakeMessages(side_effects)


async def test_extract_maps_extraction_result_into_typed_knowledge_objects_and_relations():
    client = _FakeClient([_sample_result()])
    extractor = ClaudeExtractor(client=client, model="claude-opus-5")

    outcome = await extractor.extract([_sample_chunk()], _sample_document(), _sample_source())

    # 2 entities + 2 typed items = 4 objects, none of them the old flat shape.
    assert len(outcome.objects) == 4
    entities = [ko for ko in outcome.objects if ko.type == KnowledgeObjectType.ENTITY]
    assert {e.content["name"] for e in entities} == {"Acme Corp", "the Board"}

    issue = next(ko for ko in outcome.objects if ko.type == KnowledgeObjectType.ISSUE)
    decision = next(ko for ko in outcome.objects if ko.type == KnowledgeObjectType.DECISION)
    assert issue.content["statement"] == "Regulatory approval is still pending."
    assert decision.content["statement"] == "The Board approved the merger."
    for ko in outcome.objects:
        assert ko.topics == ["contracts"]
        assert ko.document_type == "contract"
        assert ko.model_used == "claude-opus-5"
        assert ko.source.source_id == "s1"
        assert ko.document.document_id == "d1"
        assert ko.evidence[0].chunk_id == "c1"
        assert ko.pipeline_version
        assert ko.source_hash == "deadbeef"

    # entity-entity relation resolved to the two ENTITY object ids.
    acme_id = next(ko.knowledge_object_id for ko in entities if ko.content["name"] == "Acme Corp")
    board_id = next(ko.knowledge_object_id for ko in entities if ko.content["name"] == "the Board")
    related_to = [r for r in outcome.relations if r.type == RelationType.RELATED_TO and r.subject_id == acme_id]
    assert related_to and related_to[0].object_id == board_id and related_to[0].label == "reports to"

    # item -> entity MENTIONS relations.
    mentions = {(r.subject_id, r.object_id) for r in outcome.relations if r.type == RelationType.MENTIONS}
    assert (issue.knowledge_object_id, acme_id) in mentions
    assert (decision.knowledge_object_id, board_id) in mentions

    # decision caused_by issue.
    caused_by = [r for r in outcome.relations if r.type == RelationType.CAUSED_BY]
    assert caused_by == [
        r for r in caused_by if r.subject_id == decision.knowledge_object_id and r.object_id == issue.knowledge_object_id
    ]
    assert len(caused_by) == 1
    assert client.messages.calls == 1


async def test_extract_over_a_multi_chunk_bundle_grounds_evidence_in_every_chunk():
    client = _FakeClient([_sample_result(), _sample_result()])
    extractor = ClaudeExtractor(client=client, model="claude-opus-5")

    second_chunk = _sample_chunk().model_copy(update={"chunk_id": "c2", "sequence_index": 1})
    outcome = await extractor.extract([_sample_chunk(), second_chunk], _sample_document(), _sample_source())

    assert len(outcome.objects) == 4
    for ko in outcome.objects:
        assert [e.chunk_id for e in ko.evidence] == ["c1", "c2"]
    # bundling changes the id space (a KO from a [c1, c2] bundle isn't
    # confusable with one from a solo [c1] call).
    solo_ids = {ko.knowledge_object_id for ko in (await extractor.extract([_sample_chunk()], _sample_document(), _sample_source())).objects}
    assert solo_ids.isdisjoint({ko.knowledge_object_id for ko in outcome.objects})


async def test_chunk_with_no_entities_or_items_falls_back_to_one_fact_from_summary():
    empty_result = ExtractionResult(summary="Just connective prose.", document_type="note")
    client = _FakeClient([empty_result])
    extractor = ClaudeExtractor(client=client)

    outcome = await extractor.extract([_sample_chunk()], _sample_document(), _sample_source())

    assert len(outcome.objects) == 1
    assert outcome.objects[0].type == KnowledgeObjectType.FACT
    assert outcome.objects[0].content["statement"] == "Just connective prose."
    assert outcome.relations == []


async def test_retries_once_on_rate_limit_then_succeeds():
    client = _FakeClient([_rate_limit_error(), _sample_result()])
    extractor = ClaudeExtractor(client=client)

    outcome = await extractor.extract([_sample_chunk()], _sample_document(), _sample_source())

    assert len(outcome.objects) == 4
    assert client.messages.calls == 2


async def test_bad_request_is_not_retried_and_raises_knowledge_extraction_error():
    from knowledge_forge.exceptions import KnowledgeExtractionError

    client = _FakeClient([_bad_request_error()])
    extractor = ClaudeExtractor(client=client)

    with pytest.raises(KnowledgeExtractionError):
        await extractor.extract([_sample_chunk()], _sample_document(), _sample_source())

    assert client.messages.calls == 1
