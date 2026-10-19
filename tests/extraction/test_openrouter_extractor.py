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
from knowledge_forge.models.knowledge_object import KnowledgeObjectType
from knowledge_forge.models.relation import RelationType
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
        entities=[{"name": "Acme Corp", "entity_type": "ORG"}, {"name": "the Board", "entity_type": "ORG"}],
        relations=[{"subject": "Acme Corp", "predicate": "reports to", "object": "the Board"}],
        items=[
            {"type": "issue", "statement": "Regulatory approval is still pending.", "related_entities": ["Acme Corp"]},
            {
                "type": "decision",
                "statement": "The Board approved the merger.",
                "related_entities": ["the Board"],
                "caused_by_index": 0,
            },
        ],
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


async def test_extract_maps_extraction_result_into_typed_knowledge_objects_and_relations():
    client = _FakeClient([_chat_response(_sample_result_json())])
    extractor = OpenRouterExtractor(client=client, model="anthropic/claude-haiku-4.5")

    outcome = await extractor.extract([_sample_chunk()], _sample_document(), _sample_source())

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
        assert ko.model_used == "anthropic/claude-haiku-4.5"
        assert ko.source.source_id == "s1"
        assert ko.document.document_id == "d1"
        assert ko.evidence[0].chunk_id == "c1"

    caused_by = [r for r in outcome.relations if r.type == RelationType.CAUSED_BY]
    assert len(caused_by) == 1
    assert caused_by[0].subject_id == decision.knowledge_object_id
    assert caused_by[0].object_id == issue.knowledge_object_id

    assert client.chat.completions.calls == 1
    # response_format is present and carries the ExtractionResult schema
    fmt = client.chat.completions.last_kwargs["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    # thinking off, in the serving backend's own parameter name (chat_provider.reasoning_extra_body)
    assert client.chat.completions.last_kwargs["extra_body"] == {"reasoning": {"enabled": False}}


def test_reasoning_off_uses_chat_template_kwargs_for_a_self_hosted_backend():
    extractor = OpenRouterExtractor(client=_FakeClient([]), base_url="http://localhost:8001/v1")
    assert extractor._extra_body == {"chat_template_kwargs": {"enable_thinking": False}}


async def test_retries_once_on_rate_limit_then_succeeds():
    client = _FakeClient([_rate_limit_error(), _chat_response(_sample_result_json())])
    extractor = OpenRouterExtractor(client=client)

    outcome = await extractor.extract([_sample_chunk()], _sample_document(), _sample_source())

    assert len(outcome.objects) == 4
    assert client.chat.completions.calls == 2


async def test_bad_request_is_not_retried_and_raises_knowledge_extraction_error():
    client = _FakeClient([_bad_request_error()])
    extractor = OpenRouterExtractor(client=client)

    with pytest.raises(KnowledgeExtractionError):
        await extractor.extract([_sample_chunk()], _sample_document(), _sample_source())

    assert client.chat.completions.calls == 1


def _schema_nodes(node: dict):
    """Every schema node, skipping the `properties`/`$defs` name maps themselves."""
    yield node
    for key, value in node.items():
        children = value.values() if key in ("properties", "$defs") else value if isinstance(value, list) else [value]
        for child in children:
            if isinstance(child, dict):
                yield from _schema_nodes(child)


def test_strict_json_schema_drops_title_and_default_and_closes_every_object():
    from knowledge_forge.extraction.openrouter_extractor import _to_strict_json_schema

    original = ExtractionResult.model_json_schema()
    strict = _to_strict_json_schema(original)

    for node in _schema_nodes(strict):
        assert "title" not in node and "default" not in node
        if node.get("type") == "object" and "properties" in node:
            assert node["additionalProperties"] is False
            assert node["required"] == list(node["properties"])
    assert "title" in original  # the input is deep-copied, not patched in place

def test_strict_json_schema_keeps_a_field_literally_named_title():
    from knowledge_forge.extraction.openrouter_extractor import _to_strict_json_schema

    schema = {"type": "object", "title": "Doc", "properties": {"title": {"type": "string", "title": "Title"}}}
    assert _to_strict_json_schema(schema) == {
        "type": "object",
        "properties": {"title": {"type": "string"}},
        "additionalProperties": False,
        "required": ["title"],
    }
