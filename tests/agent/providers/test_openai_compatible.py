import json
from types import SimpleNamespace

import httpx2
import openai
import pytest

from knowledge_forge.agent.providers.base import Message, ToolCall, ToolSpec
from knowledge_forge.agent.providers.openai_compatible import OpenAICompatibleChatProvider
from knowledge_forge.exceptions import AgentError

_REQUEST = httpx2.Request("POST", "https://openrouter.ai/api/v1/chat/completions")


def _authentication_error() -> openai.AuthenticationError:
    response = httpx2.Response(401, request=_REQUEST, json={"error": {"message": "invalid api key"}})
    return openai.AuthenticationError("invalid api key", response=response, body=None)


def _tool_call(id_: str, name: str, arguments: dict):
    return SimpleNamespace(id=id_, function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))


def _completion(content, tool_calls=None, finish_reason="stop"):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice])


class _FakeCompletions:
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
        self.chat = SimpleNamespace(completions=_FakeCompletions(side_effects))


def _tool() -> ToolSpec:
    return ToolSpec(
        name="file_search",
        description="d",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        handler=None,
    )


async def test_plain_text_reply_has_no_tool_calls_and_end_turn():
    client = _FakeClient([_completion("hello there")])
    provider = OpenAICompatibleChatProvider(client=client, model="anthropic/claude-x")

    turn = await provider.send([Message(role="user", content="hi")], [], "sys")

    assert turn.text == "hello there"
    assert turn.tool_calls == []
    assert turn.stop_reason == "end_turn"
    sent_messages = client.chat.completions.calls[0]["messages"]
    assert sent_messages[0] == {"role": "system", "content": "sys"}
    assert "tools" not in client.chat.completions.calls[0]


async def test_tool_call_becomes_a_tool_call_with_parsed_json_arguments():
    client = _FakeClient(
        [_completion(None, tool_calls=[_tool_call("call_1", "file_search", {"query": "auth"})], finish_reason="tool_calls")]
    )
    provider = OpenAICompatibleChatProvider(client=client)

    turn = await provider.send([Message(role="user", content="hi")], [_tool()], "sys")

    assert turn.tool_calls == [ToolCall(id="call_1", name="file_search", arguments={"query": "auth"})]
    assert turn.stop_reason == "tool_use"
    sent_tools = client.chat.completions.calls[0]["tools"]
    assert sent_tools == [
        {"type": "function", "function": {"name": "file_search", "description": "d", "parameters": _tool().input_schema}}
    ]


async def test_length_finish_reason_maps_to_max_tokens_when_no_tool_calls():
    client = _FakeClient([_completion("truncated...", finish_reason="length")])
    provider = OpenAICompatibleChatProvider(client=client)

    turn = await provider.send([Message(role="user", content="hi")], [], "sys")

    assert turn.stop_reason == "max_tokens"


async def test_authentication_error_is_wrapped_with_credential_hint():
    client = _FakeClient([_authentication_error()])
    provider = OpenAICompatibleChatProvider(client=client)

    with pytest.raises(AgentError):
        await provider.send([Message(role="user", content="hi")], [], "sys")
