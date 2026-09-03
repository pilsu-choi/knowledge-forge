from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from knowledge_forge.agent.providers.anthropic import AnthropicChatProvider
from knowledge_forge.agent.providers.base import Message, ToolCall, ToolSpec
from knowledge_forge.exceptions import AgentError

_REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def _bad_request_error() -> anthropic.BadRequestError:
    response = httpx2.Response(400, request=_REQUEST, json={"error": {"message": "bad input"}})
    return anthropic.BadRequestError("bad input", response=response, body=None)


def _text_block(text: str):
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(id_: str, name: str, input_: dict):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=input_)


def _response(content: list, stop_reason: str = "end_turn"):
    return SimpleNamespace(content=content, stop_reason=stop_reason)


class _FakeMessages:
    def __init__(self, side_effects):
        self._side_effects = list(side_effects)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        effect = self._side_effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return effect


class _FakeClient:
    def __init__(self, side_effects):
        self.messages = _FakeMessages(side_effects)


def _tool() -> ToolSpec:
    return ToolSpec(
        name="file_search",
        description="d",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        handler=None,
    )


async def test_plain_text_reply_has_no_tool_calls_and_end_turn():
    client = _FakeClient([_response([_text_block("hello there")])])
    provider = AnthropicChatProvider(client=client, model="claude-x")

    turn = await provider.send([Message(role="user", content="hi")], [], "sys")

    assert turn.text == "hello there"
    assert turn.tool_calls == []
    assert turn.stop_reason == "end_turn"
    assert client.messages.calls[0]["system"] == "sys"
    assert "tools" not in client.messages.calls[0]  # no tools passed -> omitted, not an empty list


async def test_tool_use_block_becomes_a_tool_call_with_stop_reason_tool_use():
    client = _FakeClient([_response([_tool_use_block("call_1", "file_search", {"query": "auth"})], stop_reason="tool_use")])
    provider = AnthropicChatProvider(client=client)

    turn = await provider.send([Message(role="user", content="hi")], [_tool()], "sys")

    assert turn.text is None
    assert turn.tool_calls == [ToolCall(id="call_1", name="file_search", arguments={"query": "auth"})]
    assert turn.stop_reason == "tool_use"
    sent_tools = client.messages.calls[0]["tools"]
    assert sent_tools == [{"name": "file_search", "description": "d", "input_schema": _tool().input_schema}]


async def test_max_tokens_stop_reason_is_surfaced_when_there_are_no_tool_calls():
    client = _FakeClient([_response([_text_block("truncated...")], stop_reason="max_tokens")])
    provider = AnthropicChatProvider(client=client)

    turn = await provider.send([Message(role="user", content="hi")], [], "sys")

    assert turn.stop_reason == "max_tokens"


async def test_non_retryable_api_error_is_wrapped_in_agent_error():
    client = _FakeClient([_bad_request_error()])
    provider = AnthropicChatProvider(client=client)

    with pytest.raises(AgentError):
        await provider.send([Message(role="user", content="hi")], [], "sys")
