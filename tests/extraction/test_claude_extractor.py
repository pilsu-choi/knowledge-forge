from datetime import datetime, timezone
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from knowledge_forge.extraction.claude_extractor import ClaudeExtractor
from knowledge_forge.extraction.schemas import ExtractionEntity, ExtractionRelation, ExtractionResult
from knowledge_forge.models.chunk import Chunk, HierarchyLevel
from knowledge_forge.models.document import Document, DocumentMetadata, TextBlock
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
        facts=["Fact one."],
        entities=[ExtractionEntity(name="Acme Corp", type="ORG")],
        relations=[ExtractionRelation(subject="Acme Corp", predicate="signed", object="the agreement")],
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

    def parse(self, **kwargs):
        self.calls += 1
        effect = self._side_effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return SimpleNamespace(parsed_output=effect)


class _FakeClient:
    def __init__(self, side_effects):
        self.messages = _FakeMessages(side_effects)


def test_extract_maps_extraction_result_into_knowledge_object():
    client = _FakeClient([_sample_result()])
    extractor = ClaudeExtractor(client=client, model="claude-opus-5")

    ko = extractor.extract(_sample_chunk(), _sample_document(), _sample_source())

    assert ko.summary == "A short summary."
    assert ko.facts == ["Fact one."]
    assert ko.entities[0].name == "Acme Corp"
    assert ko.relations[0].predicate == "signed"
    assert ko.topics == ["contracts"]
    assert ko.document_type == "contract"
    assert ko.model_used == "claude-opus-5"
    assert ko.source.source_id == "s1"
    assert ko.document.document_id == "d1"
    assert ko.chunk.chunk_id == "c1"
    assert client.messages.calls == 1


def test_retries_once_on_rate_limit_then_succeeds():
    client = _FakeClient([_rate_limit_error(), _sample_result()])
    extractor = ClaudeExtractor(client=client)

    ko = extractor.extract(_sample_chunk(), _sample_document(), _sample_source())

    assert ko.summary == "A short summary."
    assert client.messages.calls == 2


def test_bad_request_is_not_retried_and_raises_knowledge_extraction_error():
    from knowledge_forge.exceptions import KnowledgeExtractionError

    client = _FakeClient([_bad_request_error()])
    extractor = ClaudeExtractor(client=client)

    with pytest.raises(KnowledgeExtractionError):
        extractor.extract(_sample_chunk(), _sample_document(), _sample_source())

    assert client.messages.calls == 1
