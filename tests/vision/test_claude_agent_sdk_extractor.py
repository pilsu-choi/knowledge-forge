import base64

import pytest
from claude_agent_sdk import CLINotFoundError, ResultError, ResultMessage

from knowledge_forge.exceptions import KnowledgeExtractionError
from knowledge_forge.vision.claude_agent_sdk_extractor import ClaudeAgentSDKVisionExtractor
from knowledge_forge.vision.prompts import FIGURE_PROMPT, TABLE_PROMPT, TRANSCRIBE_PROMPT


def _result_message(subtype: str, result: str | None) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=1,
        duration_api_ms=1,
        is_error=subtype != "success",
        num_turns=1,
        session_id="s",
        result=result,
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


class _FakeQuery:
    """Same shape as tests/extraction/test_claude_agent_sdk_extractor.py's
    _FakeQuery - each call pops one scripted effect, and captures the kwargs
    (including the `prompt` async generator) it was invoked with."""

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


async def _drain_prompt(query_fn: _FakeQuery) -> list[dict]:
    return [item async for item in query_fn.last_kwargs["prompt"]]


async def test_extract_table_markdown_sends_an_image_content_block_and_returns_the_result_text():
    query_fn = _FakeQuery([_result_message("success", "| a | b |\n|---|---|\n| 1 | 2 |")])
    extractor = ClaudeAgentSDKVisionExtractor(model="claude-opus-5", query_fn=query_fn)

    result = await extractor.extract_table_markdown(b"fake-png-bytes", media_type="image/png")

    assert result == "| a | b |\n|---|---|\n| 1 | 2 |"
    assert query_fn.calls == 1
    options = query_fn.last_kwargs["options"]
    assert options.tools == []
    assert options.model == "claude-opus-5"

    messages = await _drain_prompt(query_fn)
    assert len(messages) == 1
    content = messages[0]["message"]["content"]
    image_block = next(b for b in content if b["type"] == "image")
    text_block = next(b for b in content if b["type"] == "text")
    assert image_block["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": base64.standard_b64encode(b"fake-png-bytes").decode("ascii"),
    }
    assert text_block["text"] == TABLE_PROMPT


async def test_describe_figure_and_transcribe_image_use_their_own_prompts():
    for method_name, prompt in (("describe_figure", FIGURE_PROMPT), ("transcribe_image", TRANSCRIBE_PROMPT)):
        query_fn = _FakeQuery([_result_message("success", "a description")])
        extractor = ClaudeAgentSDKVisionExtractor(query_fn=query_fn)

        result = await getattr(extractor, method_name)(b"bytes")

        assert result == "a description"
        messages = await _drain_prompt(query_fn)
        text_block = next(b for b in messages[0]["message"]["content"] if b["type"] == "text")
        assert text_block["text"] == prompt


async def test_retries_once_on_retryable_api_error_then_succeeds():
    query_fn = _FakeQuery(
        [
            _result_error(api_error_status=529),
            _result_message("success", "recovered"),
        ]
    )
    extractor = ClaudeAgentSDKVisionExtractor(query_fn=query_fn)

    result = await extractor.describe_figure(b"bytes")

    assert result == "recovered"
    assert query_fn.calls == 2


async def test_non_retryable_result_error_raises_knowledge_extraction_error():
    query_fn = _FakeQuery([_result_error(api_error_status=400)])
    extractor = ClaudeAgentSDKVisionExtractor(query_fn=query_fn)

    with pytest.raises(KnowledgeExtractionError):
        await extractor.describe_figure(b"bytes")

    assert query_fn.calls == 1


async def test_cli_not_found_raises_knowledge_extraction_error_with_credential_hint():
    query_fn = _FakeQuery([CLINotFoundError(cli_path="claude")])
    extractor = ClaudeAgentSDKVisionExtractor(query_fn=query_fn)

    with pytest.raises(KnowledgeExtractionError) as exc_info:
        await extractor.transcribe_image(b"bytes")

    assert "CLAUDE_CODE_OAUTH_TOKEN" in str(exc_info.value)
    assert query_fn.calls == 1


async def test_success_subtype_with_no_result_text_raises_knowledge_extraction_error():
    query_fn = _FakeQuery([_result_message("success", None)])
    extractor = ClaudeAgentSDKVisionExtractor(query_fn=query_fn)

    with pytest.raises(KnowledgeExtractionError):
        await extractor.describe_figure(b"bytes")
