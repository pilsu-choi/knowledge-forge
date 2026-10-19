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


def _raw_tool_call(id_: str, name: str, arguments: str):
    """`_tool_call`과 달리 인자를 dict가 아니라 이미 JSON 문자열로 넣어 그대로(깨져도)
    싣는다 - Qwen에서 관측된 깨진 JSON을 재현하기 위한 것."""
    return SimpleNamespace(id=id_, function=SimpleNamespace(name=name, arguments=arguments))


async def test_malformed_json_arguments_become_an_error_sentinel_instead_of_raising():
    """결함11(`wiki/search-code-review-2026-09-14.md`): 무방비 `json.loads`가 Qwen의 깨진
    JSON에서 `JSONDecodeError`를 올려 `provider.send()`를(나아가 `run_agent`를) 죽였다.
    이제 예외 대신 `agent/loop.py::INVALID_JSON_ARGUMENTS_KEY` 하나만 든 인자를 돌려줘
    루프가 살아남는다."""
    from knowledge_forge.agent.loop import INVALID_JSON_ARGUMENTS_KEY

    client = _FakeClient(
        [
            _completion(
                None,
                tool_calls=[_raw_tool_call("call_1", "search", "{query: not valid json")],
                finish_reason="tool_calls",
            )
        ]
    )
    provider = OpenAICompatibleChatProvider(client=client)

    turn = await provider.send([Message(role="user", content="hi")], [_tool()], "sys")

    assert turn.tool_calls[0].id == "call_1"
    assert turn.tool_calls[0].name == "search"
    assert INVALID_JSON_ARGUMENTS_KEY in turn.tool_calls[0].arguments


async def test_non_object_json_arguments_also_become_an_error_sentinel():
    from knowledge_forge.agent.loop import INVALID_JSON_ARGUMENTS_KEY

    client = _FakeClient(
        [_completion(None, tool_calls=[_raw_tool_call("call_1", "search", "[1, 2, 3]")], finish_reason="tool_calls")]
    )
    provider = OpenAICompatibleChatProvider(client=client)

    turn = await provider.send([Message(role="user", content="hi")], [_tool()], "sys")

    assert INVALID_JSON_ARGUMENTS_KEY in turn.tool_calls[0].arguments


# --- reasoning / max_tokens / 잘림 / 스트리밍 / 로컬 키 ---


def _completion_with_reasoning(content, reasoning_field, reasoning, finish_reason="stop"):
    message = SimpleNamespace(content=content, tool_calls=None, **{reasoning_field: reasoning})
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish_reason)])


@pytest.mark.parametrize("field", ["reasoning_content", "reasoning"])
async def test_reasoning_field_is_read_from_either_name(field):
    client = _FakeClient([_completion_with_reasoning("답", field, "생각 중")])
    provider = OpenAICompatibleChatProvider(client=client)

    turn = await provider.send([Message(role="user", content="hi")], [], "sys")

    assert turn.reasoning == "생각 중"
    assert turn.text == "답"


async def test_reasoning_is_echoed_on_tool_call_messages_only_when_enabled():
    history = [
        Message(role="user", content="q"),
        Message(
            role="assistant",
            content=None,
            reasoning="plan",
            tool_calls=[ToolCall(id="c1", name="file_search", arguments={"query": "a"})],
        ),
        Message(role="tool", content="r", tool_call_id="c1"),
    ]
    on = _FakeClient([_completion("ok")])
    await OpenAICompatibleChatProvider(client=on, reasoning=True).send(history, [_tool()], "sys")
    off = _FakeClient([_completion("ok")])
    await OpenAICompatibleChatProvider(client=off).send(history, [_tool()], "sys")

    assert on.chat.completions.calls[0]["messages"][2]["reasoning"] == "plan"
    assert "reasoning" not in off.chat.completions.calls[0]["messages"][2]


@pytest.mark.parametrize(
    "base_url, reasoning, expected",
    [
        ("https://openrouter.ai/api/v1", False, {"reasoning": {"enabled": False}}),
        ("https://openrouter.ai/api/v1", True, {"reasoning": {"enabled": True}}),
        ("http://localhost:8000/v1", False, {"chat_template_kwargs": {"enable_thinking": False}}),
        ("http://10.0.0.5:8000/v1", True, {"chat_template_kwargs": {"enable_thinking": True}}),
        ("https://api.openai.com/v1", False, None),
    ],
)
async def test_reasoning_toggle_uses_the_serving_specific_parameter(base_url, reasoning, expected):
    client = _FakeClient([_completion("ok")])
    provider = OpenAICompatibleChatProvider(client=client, base_url=base_url, reasoning=reasoning)

    await provider.send([Message(role="user", content="hi")], [], "sys")

    assert client.chat.completions.calls[0].get("extra_body") == expected


async def test_sampling_and_max_tokens_settings_reach_the_request():
    client = _FakeClient([_completion("ok")])
    provider = OpenAICompatibleChatProvider(client=client, max_tokens=777, temperature=0.2, seed=7)

    await provider.send([Message(role="user", content="hi")], [], "sys")

    call = client.chat.completions.calls[0]
    assert (call["max_tokens"], call["temperature"], call["seed"]) == (777, 0.2, 7)
    plain = _FakeClient([_completion("ok")])
    await OpenAICompatibleChatProvider(client=plain).send([Message(role="user", content="hi")], [], "sys")
    assert "temperature" not in plain.chat.completions.calls[0]
    assert "seed" not in plain.chat.completions.calls[0]


async def test_tool_call_cut_off_by_length_is_reported_as_truncation_not_bad_json():
    from knowledge_forge.agent.loop import INVALID_JSON_ARGUMENTS_KEY

    client = _FakeClient(
        [_completion(None, tool_calls=[_raw_tool_call("c1", "search", '{"query": "잘리는')], finish_reason="length")]
    )
    provider = OpenAICompatibleChatProvider(client=client)

    turn = await provider.send([Message(role="user", content="hi")], [_tool()], "sys")

    reason = turn.tool_calls[0].arguments[INVALID_JSON_ARGUMENTS_KEY]
    assert "max_tokens" in reason and "length" in reason


async def test_reasoning_only_reply_is_not_returned_as_an_empty_answer():
    from knowledge_forge.agent.loop import run_agent

    client = _FakeClient([_completion_with_reasoning("", "reasoning", "생각만 하다 끝남", finish_reason="length")])
    provider = OpenAICompatibleChatProvider(client=client)

    with pytest.raises(AgentError, match="thinking"):
        await run_agent("q", [], "sys", provider)


def _chunk(content=None, reasoning=None, tool_calls=None, finish_reason=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls, reasoning=reasoning)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)])


def _tc_delta(index, id_=None, name=None, arguments=None):
    return SimpleNamespace(index=index, id=id_, function=SimpleNamespace(name=name, arguments=arguments))


class _Stream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for c in self._chunks:
            yield c


async def test_send_stream_emits_text_deltas_and_records_ttft():
    client = _FakeClient([_Stream([_chunk(reasoning="흠"), _chunk("안녕"), _chunk("하세요"), _chunk(finish_reason="stop")])])
    provider = OpenAICompatibleChatProvider(client=client)
    seen: list[str] = []

    turn = await provider.send_stream([Message(role="user", content="hi")], [], "sys", seen.append)

    assert seen == ["안녕", "하세요"]
    assert turn.text == "안녕하세요"
    assert turn.reasoning == "흠"
    assert turn.stop_reason == "end_turn"
    assert turn.ttft_s is not None
    assert client.chat.completions.calls[0]["stream"] is True


async def test_send_stream_assembles_tool_calls_from_fragments():
    client = _FakeClient(
        [
            _Stream(
                [
                    _chunk(tool_calls=[_tc_delta(0, "c1", "search", '{"que')]),
                    _chunk(tool_calls=[_tc_delta(0, None, None, 'ry": "a"}')]),
                    _chunk(finish_reason="tool_calls"),
                ]
            )
        ]
    )
    provider = OpenAICompatibleChatProvider(client=client)

    turn = await provider.send_stream([Message(role="user", content="hi")], [_tool()], "sys", lambda t: None)

    assert turn.tool_calls == [ToolCall(id="c1", name="search", arguments={"query": "a"})]
    assert turn.stop_reason == "tool_use"
    assert turn.ttft_s is None


async def test_run_agent_streams_through_on_text_with_a_provider_that_cannot_stream():
    """기본 `ChatProvider.send_stream`은 send 결과를 한 번에 흘린다 - 구현체가 스트리밍을 몰라도 계약은 유지."""
    from knowledge_forge.agent.loop import run_agent
    from knowledge_forge.agent.providers.base import AssistantTurn, ChatProvider

    class _P(ChatProvider):
        async def send(self, messages, tools, system):
            return AssistantTurn(text="final", tool_calls=[], stop_reason="end_turn")

    seen: list[str] = []
    answer, _ = await run_agent("q", [], "sys", _P(), on_text=seen.append)

    assert answer == "final"
    assert seen == ["final"]


def test_local_base_urls_skip_the_api_key_preflight(monkeypatch):
    from knowledge_forge.agent import auth
    from knowledge_forge.config import settings

    for var in ("OPENROUTER_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(settings, "agent_provider", "openai_compatible")
    results = {}
    for url in (
        "http://localhost:8000/v1",
        "http://127.0.0.1:8000/v1",
        "http://192.168.0.7:8000/v1",
        "http://vllm:8000/v1",
        "https://openrouter.ai/api/v1",
        "http://8.8.8.8/v1",
    ):
        monkeypatch.setattr(settings, "agent_openai_compatible_base_url", url)
        results[url] = auth.has_resolvable_agent_credentials()
    assert [u for u, ok in results.items() if ok] == [
        "http://localhost:8000/v1",
        "http://127.0.0.1:8000/v1",
        "http://192.168.0.7:8000/v1",
        "http://vllm:8000/v1",
    ]
