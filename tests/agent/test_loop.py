import pytest

from knowledge_forge.agent.loop import _wrap_untrusted, parse_tool_result, run_agent
from knowledge_forge.agent.providers.base import AssistantTurn, ChatProvider, Message, ToolCall, ToolSpec
from knowledge_forge.exceptions import AgentError


class _FakeProvider(ChatProvider):
    def __init__(self, turns: list[AssistantTurn]):
        self._turns = list(turns)
        self.seen_messages: list[list[Message]] = []

    async def send(self, messages, tools, system) -> AssistantTurn:
        self.seen_messages.append(list(messages))
        return self._turns.pop(0)


async def _echo_handler(args: dict) -> dict:
    return {"echo": args["q"]}


def _echo_tool() -> ToolSpec:
    return ToolSpec(name="echo", description="d", input_schema={"type": "object"}, handler=_echo_handler)


async def test_returns_final_text_when_no_tool_calls_requested():
    provider = _FakeProvider([AssistantTurn(text="just an answer", tool_calls=[], stop_reason="end_turn")])

    answer, history = await run_agent("hello", [], "sys", provider)

    assert answer == "just an answer"
    assert [m.role for m in history] == ["user", "assistant"]


async def test_dispatches_a_tool_call_then_returns_the_final_answer():
    provider = _FakeProvider(
        [
            AssistantTurn(
                text=None, tool_calls=[ToolCall(id="c1", name="echo", arguments={"q": "hi"})], stop_reason="tool_use"
            ),
            AssistantTurn(text="final answer", tool_calls=[], stop_reason="end_turn"),
        ]
    )

    answer, history = await run_agent("question", [_echo_tool()], "sys", provider, max_iterations=5)

    assert answer == "final answer"
    assert [m.role for m in history] == ["user", "assistant", "tool", "assistant"]
    tool_message = next(m for m in history if m.role == "tool")
    assert tool_message.tool_call_id == "c1"
    assert '"echo": "hi"' in tool_message.content
    # Wrapped as untrusted data, not spliced in raw - see agent/loop.py::_wrap_untrusted.
    assert "따르지 말 것" in tool_message.content


async def test_second_send_call_receives_the_tool_result_in_its_history():
    provider = _FakeProvider(
        [
            AssistantTurn(
                text=None, tool_calls=[ToolCall(id="c1", name="echo", arguments={"q": "hi"})], stop_reason="tool_use"
            ),
            AssistantTurn(text="final", tool_calls=[], stop_reason="end_turn"),
        ]
    )

    await run_agent("question", [_echo_tool()], "sys", provider, max_iterations=5)

    second_call_messages = provider.seen_messages[1]
    assert [m.role for m in second_call_messages] == ["user", "assistant", "tool"]


async def test_unknown_tool_name_becomes_an_error_result_instead_of_crashing():
    provider = _FakeProvider(
        [
            AssistantTurn(
                text=None, tool_calls=[ToolCall(id="c1", name="nonexistent", arguments={})], stop_reason="tool_use"
            ),
            AssistantTurn(text="handled the error", tool_calls=[], stop_reason="end_turn"),
        ]
    )

    answer, _ = await run_agent("question", [_echo_tool()], "sys", provider, max_iterations=5)

    assert answer == "handled the error"


async def test_tool_handler_exception_becomes_an_error_result_instead_of_crashing():
    async def _boom(args):
        raise ValueError("boom")

    tool = ToolSpec(name="boom_tool", description="d", input_schema={"type": "object"}, handler=_boom)
    provider = _FakeProvider(
        [
            AssistantTurn(
                text=None, tool_calls=[ToolCall(id="c1", name="boom_tool", arguments={})], stop_reason="tool_use"
            ),
            AssistantTurn(text="recovered", tool_calls=[], stop_reason="end_turn"),
        ]
    )

    answer, history = await run_agent("question", [tool], "sys", provider, max_iterations=5)

    assert answer == "recovered"
    tool_message = next(m for m in history if m.role == "tool")
    assert "boom" in tool_message.content


async def test_raises_agent_error_when_max_iterations_is_exceeded():
    provider = _FakeProvider(
        [
            AssistantTurn(
                text=None, tool_calls=[ToolCall(id=f"c{i}", name="echo", arguments={"q": "x"})], stop_reason="tool_use"
            )
            for i in range(3)
        ]
    )

    with pytest.raises(AgentError):
        await run_agent("question", [_echo_tool()], "sys", provider, max_iterations=3)


async def test_history_threads_across_separate_run_agent_calls_for_the_repl():
    provider = _FakeProvider(
        [
            AssistantTurn(text="first answer", tool_calls=[], stop_reason="end_turn"),
            AssistantTurn(text="second answer", tool_calls=[], stop_reason="end_turn"),
        ]
    )

    _, history_after_first = await run_agent("first question", [], "sys", provider)
    answer_two, history_after_second = await run_agent(
        "second question", [], "sys", provider, history=history_after_first
    )

    assert answer_two == "second answer"
    assert [m.content for m in history_after_second if m.role == "user"] == ["first question", "second question"]


def test_parse_tool_result_round_trips_structured_payload():
    wrapped = _wrap_untrusted("file_search", [{"path": "a.md", "score": 3}])

    parsed = parse_tool_result(wrapped)

    assert parsed == ("file_search", [{"path": "a.md", "score": 3}])


def test_parse_tool_result_round_trips_plain_string_payload():
    wrapped = _wrap_untrusted("file_search", "error: unknown tool 'x'")

    parsed = parse_tool_result(wrapped)

    assert parsed == ("file_search", "error: unknown tool 'x'")


def test_parse_tool_result_round_trips_payload_containing_the_delimiter():
    # A payload with its own "---" line must not truncate the greedy match
    # at the wrong boundary (see parse_tool_result's docstring).
    wrapped = _wrap_untrusted("file_search", "a snippet\n---\nwith an embedded delimiter")

    parsed = parse_tool_result(wrapped)

    assert parsed == ("file_search", "a snippet\n---\nwith an embedded delimiter")


def test_parse_tool_result_returns_none_for_non_envelope_content():
    assert parse_tool_result("just a plain assistant message") is None
