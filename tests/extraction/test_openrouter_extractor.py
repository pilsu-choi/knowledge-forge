from datetime import datetime, timezone
from types import SimpleNamespace

import httpx2
import openai
import pytest

from knowledge_forge.exceptions import KnowledgeExtractionError
from knowledge_forge.extraction.openrouter_extractor import OpenRouterExtractor
from knowledge_forge.extraction.schemas import ExtractionResult
from knowledge_forge.models.chunk import Chunk, HierarchyLevel
from knowledge_forge.models.document import Document, DocumentMetadata, TextBlock
from knowledge_forge.models.source import Source, SourceRef, SourceType

_REQUEST = httpx2.Request("POST", "https://openrouter.ai/api/v1/chat/completions")


def _rate_limit_error() -> openai.RateLimitError:
    response = httpx2.Response(429, request=_REQUEST, json={"error": {"message": "slow down"}})
    return openai.RateLimitError("slow down", response=response, body=None)


def _bad_request_error() -> openai.BadRequestError:
    response = httpx2.Response(400, request=_REQUEST, json={"error": {"message": "bad input"}})
    return openai.BadRequestError("bad input", response=response, body=None)


def _sample_result_json() -> str:
    return ExtractionResult(
        summary="A short summary.",
        facts=["Fact one."],
        entities=[{"name": "Acme Corp", "type": "ORG"}],
        relations=[{"subject": "Acme Corp", "predicate": "signed", "object": "the agreement"}],
        topics=["contracts"],
        document_type="contract",
    ).model_dump_json()


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


def _chat_response(content: str):
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice])


class _FakeCompletions:
    def __init__(self, side_effects):
        self._side_effects = list(side_effects)
        self.calls = 0
        self.last_kwargs = None

    async def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        effect = self._side_effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return effect


class _FakeChat:
    def __init__(self, side_effects):
        self.completions = _FakeCompletions(side_effects)


class _FakeClient:
    def __init__(self, side_effects):
        self.chat = _FakeChat(side_effects)


async def test_extract_maps_extraction_result_into_knowledge_object():
    client = _FakeClient([_chat_response(_sample_result_json())])
    extractor = OpenRouterExtractor(client=client, model="anthropic/claude-haiku-4.5")

    ko = await extractor.extract(_sample_chunk(), _sample_document(), _sample_source())

    assert ko.summary == "A short summary."
    assert ko.facts == ["Fact one."]
    assert ko.entities[0].name == "Acme Corp"
    assert ko.relations[0].predicate == "signed"
    assert ko.topics == ["contracts"]
    assert ko.document_type == "contract"
    assert ko.model_used == "anthropic/claude-haiku-4.5"
    assert ko.source.source_id == "s1"
    assert ko.document.document_id == "d1"
    assert ko.chunk.chunk_id == "c1"
    assert client.chat.completions.calls == 1
    # response_format is present and carries the ExtractionResult schema
    fmt = client.chat.completions.last_kwargs["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True


async def test_retries_once_on_rate_limit_then_succeeds():
    client = _FakeClient([_rate_limit_error(), _chat_response(_sample_result_json())])
    extractor = OpenRouterExtractor(client=client)

    ko = await extractor.extract(_sample_chunk(), _sample_document(), _sample_source())

    assert ko.summary == "A short summary."
    assert client.chat.completions.calls == 2


async def test_bad_request_is_not_retried_and_raises_knowledge_extraction_error():
    client = _FakeClient([_bad_request_error()])
    extractor = OpenRouterExtractor(client=client)

    with pytest.raises(KnowledgeExtractionError):
        await extractor.extract(_sample_chunk(), _sample_document(), _sample_source())

    assert client.chat.completions.calls == 1
