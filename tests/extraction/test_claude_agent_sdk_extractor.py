from datetime import datetime, timezone

import pytest
from claude_agent_sdk import CLINotFoundError, ResultError, ResultMessage

from knowledge_forge.exceptions import KnowledgeExtractionError
from knowledge_forge.extraction.claude_agent_sdk_extractor import ClaudeAgentSDKExtractor
from knowledge_forge.extraction.schemas import ExtractionItem, ExtractionRelation, ExtractionResult
from knowledge_forge.models.chunk import Chunk, HierarchyLevel
from knowledge_forge.models.document import Document, DocumentMetadata, TextBlock
from knowledge_forge.models.knowledge_object import EntityContent, KnowledgeObjectType
from knowledge_forge.models.relation import RelationType
from knowledge_forge.models.source import Source, SourceRef, SourceType


def _result_message(subtype: str, structured_output) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=1,
        duration_api_ms=1,
        is_error=subtype != "success",
        num_turns=1,
        session_id="s",
        structured_output=structured_output,
    )


def _result_error(api_error_status=None, terminal_reason=None) -> ResultError:
    return ResultError(
        "Claude Code returned an error result",
        data={
            "subtype": "error_during_execution",
            "api_error_status": api_error_status,
            "terminal_reason": terminal_reason,
        },
    )


def _sample_result() -> ExtractionResult:
    return ExtractionResult(
        summary="A short summary.",
        entities=[EntityContent(name="Acme Corp", entity_type="ORG"), EntityContent(name="the Board", entity_type="ORG")],
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


class _FakeQuery:
    """Stands in for claude_agent_sdk.query(): each call pops one scripted
    effect - either a ResultMessage to yield, or an exception to raise
    partway through iteration (mirroring how query() actually raises
    ResultError/CLIConnectionError from inside the async generator)."""

    def __init__(self, side_effects):
        self._side_effects = list(side_effects)
        self.calls = 0
        self.last_kwargs = None

    async def __call__(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        effect = self._side_effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        yield effect


async def test_extract_maps_extraction_result_into_typed_knowledge_objects_and_relations():
    query_fn = _FakeQuery([_result_message("success", _sample_result().model_dump(mode="json"))])
    extractor = ClaudeAgentSDKExtractor(model="claude-opus-5", query_fn=query_fn)

    outcome = await extractor.extract([_sample_chunk()], _sample_document(), _sample_source())

    assert len(outcome.objects) == 4
    entities = [ko for ko in outcome.objects if ko.type == KnowledgeObjectType.ENTITY]
    assert {e.content["name"] for e in entities} == {"Acme Corp", "the Board"}
    issue = next(ko for ko in outcome.objects if ko.type == KnowledgeObjectType.ISSUE)
    decision = next(ko for ko in outcome.objects if ko.type == KnowledgeObjectType.DECISION)
    assert issue.content["statement"] == "Regulatory approval is still pending."
    assert decision.content["statement"] == "The Board approved the merger."
    caused_by = [r for r in outcome.relations if r.type == RelationType.CAUSED_BY]
    assert len(caused_by) == 1
    assert caused_by[0].subject_id == decision.knowledge_object_id
    assert caused_by[0].object_id == issue.knowledge_object_id
    for ko in outcome.objects:
        assert ko.model_used == "claude-opus-5"

    assert query_fn.calls == 1
    options = query_fn.last_kwargs["options"]
    assert options.tools == []
    assert options.max_turns == 3
    assert options.output_format["type"] == "json_schema"


async def test_retries_once_on_retryable_api_error_then_succeeds():
    query_fn = _FakeQuery(
        [
            _result_error(api_error_status=529),
            _result_message("success", _sample_result().model_dump(mode="json")),
        ]
    )
    extractor = ClaudeAgentSDKExtractor(query_fn=query_fn)

    outcome = await extractor.extract([_sample_chunk()], _sample_document(), _sample_source())

    assert len(outcome.objects) == 4
    assert query_fn.calls == 2


async def test_non_retryable_result_error_raises_knowledge_extraction_error():
    query_fn = _FakeQuery([_result_error(api_error_status=400)])
    extractor = ClaudeAgentSDKExtractor(query_fn=query_fn)

    with pytest.raises(KnowledgeExtractionError):
        await extractor.extract([_sample_chunk()], _sample_document(), _sample_source())

    assert query_fn.calls == 1


async def test_cli_not_found_raises_knowledge_extraction_error_with_credential_hint():
    query_fn = _FakeQuery([CLINotFoundError(cli_path="claude")])
    extractor = ClaudeAgentSDKExtractor(query_fn=query_fn)

    with pytest.raises(KnowledgeExtractionError) as exc_info:
        await extractor.extract([_sample_chunk()], _sample_document(), _sample_source())

    assert "CLAUDE_CODE_OAUTH_TOKEN" in str(exc_info.value)
    assert query_fn.calls == 1


async def test_success_subtype_with_no_structured_output_raises_knowledge_extraction_error():
    query_fn = _FakeQuery([_result_message("success", None)])
    extractor = ClaudeAgentSDKExtractor(query_fn=query_fn)

    with pytest.raises(KnowledgeExtractionError):
        await extractor.extract([_sample_chunk()], _sample_document(), _sample_source())
