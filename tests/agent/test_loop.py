import asyncio

import pytest

from knowledge_forge.agent.loop import run_agent
from knowledge_forge.agent.providers.base import AssistantTurn, ChatProvider, Message, ToolCall, ToolSpec
from knowledge_forge.config import settings
from knowledge_forge.exceptions import AgentError


@pytest.fixture(autouse=True)
def _no_prefetch_by_default(monkeypatch):
    # 기존 테스트는 모델이 직접 search를 부르는 흐름을 전제한다 - 선실행은 아래 전용 테스트에서 켠다.
    monkeypatch.setattr(settings, "agent_prefetch_search", False)


class _FakeProvider(ChatProvider):
    def __init__(self, turns: list[AssistantTurn]):
        self._turns = list(turns)
        self.seen_messages: list[list[Message]] = []
        self.seen_tools: list[list[ToolSpec]] = []

    async def send(self, messages, tools, system) -> AssistantTurn:
        self.seen_messages.append(list(messages))
        self.seen_tools.append(list(tools))
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


async def test_missing_required_argument_gets_a_readable_error_message():
    """결함(`wiki/search-code-review-2026-09-14.md` 추천 수정 순서 1): `args['query']`가 빠지면
    `str(KeyError(...))`가 `"'query'"`뿐이라 모델이 뭘 고쳐야 하는지 못 읽는다."""

    async def _needs_query(args: dict) -> dict:
        return {"echo": args["query"]}

    tool = ToolSpec(name="search", description="d", input_schema={"type": "object"}, handler=_needs_query)
    provider = _FakeProvider(
        [
            AssistantTurn(text=None, tool_calls=[ToolCall(id="c1", name="search", arguments={})], stop_reason="tool_use"),
            AssistantTurn(text="handled", tool_calls=[], stop_reason="end_turn"),
        ]
    )

    answer, history = await run_agent("question", [tool], "sys", provider, max_iterations=5)

    assert answer == "handled"
    tool_message = next(m for m in history if m.role == "tool")
    assert "missing required argument 'query'" in tool_message.content


async def test_invalid_json_arguments_sentinel_becomes_an_error_result_without_calling_the_handler():
    """`agent/providers/openai_compatible.py`가 JSON 파싱에 실패하면 `ToolCall.arguments`에
    `INVALID_JSON_ARGUMENTS_KEY` 하나만 담아 돌려준다(결함11) - `_dispatch`가 핸들러를 부르기도
    전에 이걸 도구 오류 결과로 바꿔야 한다(핸들러가 불렸다면 잘못된 인자로 또 다른 예외가 났을
    것)."""
    from knowledge_forge.agent.loop import INVALID_JSON_ARGUMENTS_KEY

    async def _should_not_be_called(args: dict) -> dict:
        raise AssertionError("handler must not run for invalid-JSON tool calls")

    tool = ToolSpec(name="search", description="d", input_schema={"type": "object"}, handler=_should_not_be_called)
    provider = _FakeProvider(
        [
            AssistantTurn(
                text=None,
                tool_calls=[ToolCall(id="c1", name="search", arguments={INVALID_JSON_ARGUMENTS_KEY: "Expecting value"})],
                stop_reason="tool_use",
            ),
            AssistantTurn(text="retried with fixed arguments", tool_calls=[], stop_reason="end_turn"),
        ]
    )

    answer, history = await run_agent("question", [tool], "sys", provider, max_iterations=5)

    assert answer == "retried with fixed arguments"
    tool_message = next(m for m in history if m.role == "tool")
    assert "error: invalid JSON arguments: Expecting value" in tool_message.content


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


def _tool_turns(n: int) -> list[AssistantTurn]:
    return [
        AssistantTurn(
            text=None, tool_calls=[ToolCall(id=f"c{i}", name="echo", arguments={"q": "x"})], stop_reason="tool_use"
        )
        for i in range(n)
    ]


async def test_exhausting_max_iterations_forces_a_final_answer_instead_of_raising():
    """CONTRACT CHANGE: hitting `max_iterations` used to raise `AgentError`,
    throwing away N rounds of successful tool calls and handing the user an
    exception instead of an answer. It now spends one extra `provider.send`
    with an EMPTY tool list - the model cannot request more tools, so it has
    to answer from what it already gathered - preceded by a `user` nudge
    saying the tool budget is spent. `AgentError` survives only for the case
    where even that forced call produces no text (next test)."""
    provider = _FakeProvider(
        [*_tool_turns(3), AssistantTurn(text="지금까지 찾은 것만으로 답하면", tool_calls=[], stop_reason="end_turn")]
    )

    answer, history = await run_agent("question", [_echo_tool()], "sys", provider, max_iterations=3)

    assert answer == "지금까지 찾은 것만으로 답하면"
    assert len(provider.seen_messages) == 4  # 3 tool rounds + 1 forced final call
    assert provider.seen_tools[-1] == []  # ...made with no tools at all
    # The nudge is a user message in the transcript (not an edited `system`),
    # so it is visible to the next `--interactive` turn too.
    assert history[-2].role == "user"
    assert "max_iterations" in (history[-2].content or "")


async def test_raises_agent_error_only_when_the_forced_final_answer_is_also_empty():
    provider = _FakeProvider([*_tool_turns(3), AssistantTurn(text=None, tool_calls=[], stop_reason="end_turn")])

    with pytest.raises(AgentError):
        await run_agent("question", [_echo_tool()], "sys", provider, max_iterations=3)


async def test_truncated_answer_is_returned_but_marked_incomplete():
    """CONTRACT CHANGE: a `stop_reason="max_tokens"` turn is a half answer
    the provider cut off mid-sentence. The loop used to return it as-is,
    indistinguishable from a complete answer. It is still returned (partial
    text beats nothing, and `run_agent`'s 2-tuple signature is fixed by its
    callers) but with an explicit marker appended, so no caller - CLI user,
    parent agent via `delegate_task` - can mistake it for a
    finished answer. The transcript keeps the model's raw text."""
    provider = _FakeProvider([AssistantTurn(text="답변이 여기서", tool_calls=[], stop_reason="max_tokens")])

    answer, history = await run_agent("question", [], "sys", provider)

    assert answer.startswith("답변이 여기서")
    assert "잘림" in answer
    assert history[-1].content == "답변이 여기서"


async def test_parallel_tool_calls_run_concurrently_and_results_keep_tool_call_order():
    """One turn emitting several `tool_use` blocks means them to run in
    parallel. `first` only finishes once `second` has started, so sequential
    dispatch cannot pass this (it would sit in the timeout instead of
    returning the payload asserted below) - and `gather` still has to hand
    the results back in `turn.tool_calls` order, which the Anthropic adapter
    relies on when it coalesces them into one user message."""
    second_started = asyncio.Event()

    async def _first(args: dict) -> dict:
        await asyncio.wait_for(second_started.wait(), timeout=5)
        return {"which": "first"}

    async def _second(args: dict) -> dict:
        second_started.set()
        return {"which": "second"}

    tools = [
        ToolSpec(name="first", description="d", input_schema={"type": "object"}, handler=_first),
        ToolSpec(name="second", description="d", input_schema={"type": "object"}, handler=_second),
    ]
    provider = _FakeProvider(
        [
            AssistantTurn(
                text=None,
                tool_calls=[
                    ToolCall(id="c1", name="first", arguments={}),
                    ToolCall(id="c2", name="second", arguments={}),
                ],
                stop_reason="tool_use",
            ),
            AssistantTurn(text="done", tool_calls=[], stop_reason="end_turn"),
        ]
    )

    _, history = await run_agent("question", tools, "sys", provider, max_iterations=5)

    tool_messages = [m for m in history if m.role == "tool"]
    assert [m.tool_call_id for m in tool_messages] == ["c1", "c2"]
    assert '"which": "first"' in (tool_messages[0].content or "")
    assert '"which": "second"' in (tool_messages[1].content or "")


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


# --- 첫 턴 search 선실행 / 반복 호출 차단 / 마크업 감지 ---------------------------


def _search_tool(calls: list[dict] | None = None) -> ToolSpec:
    async def _handler(args: dict) -> dict:
        if calls is not None:
            calls.append(args)
        return {"hits": [args["query"]]}

    return ToolSpec(name="search", description="d", input_schema={"type": "object"}, handler=_handler)


def _tool_turn(name: str, args: dict, call_id: str = "c1") -> AssistantTurn:
    return AssistantTurn(text=None, tool_calls=[ToolCall(id=call_id, name=name, arguments=args)], stop_reason="tool_use")


def _answer(text: str) -> AssistantTurn:
    return AssistantTurn(text=text, tool_calls=[], stop_reason="end_turn")


async def test_prefetch_runs_search_before_first_model_call(monkeypatch):
    monkeypatch.setattr(settings, "agent_prefetch_search", True)
    calls: list[dict] = []
    provider = _FakeProvider([_answer("done")])

    answer, history = await run_agent("질문 원문", [_search_tool(calls)], "sys", provider)

    assert answer == "done"
    assert calls == [{"query": "질문 원문"}]
    assert len(provider.seen_messages) == 1  # 모델 호출이 도구 호출 결정 턴만큼 줄었다
    first = provider.seen_messages[0]
    assert [m.role for m in first] == ["user", "assistant", "tool"]
    assert first[1].tool_calls[0].name == "search" and first[1].tool_calls[0].id == first[2].tool_call_id
    assert "따르지 말 것" in first[2].content  # 신뢰 경계 포함


async def test_prefetch_disabled_lets_the_model_call_search(monkeypatch):
    monkeypatch.setattr(settings, "agent_prefetch_search", False)
    calls: list[dict] = []
    provider = _FakeProvider([_tool_turn("search", {"query": "q"}), _answer("done")])

    await run_agent("q", [_search_tool(calls)], "sys", provider)

    assert len(provider.seen_messages) == 2
    assert calls == [{"query": "q"}]


async def test_prefetch_skipped_without_search_tool(monkeypatch):
    monkeypatch.setattr(settings, "agent_prefetch_search", True)
    provider = _FakeProvider([_answer("hi")])

    _, history = await run_agent("q", [_echo_tool()], "sys", provider)

    assert [m.role for m in history] == ["user", "assistant"]


async def test_repeated_identical_call_is_not_executed_again(monkeypatch):
    monkeypatch.setattr(settings, "agent_prefetch_search", False)
    calls: list[dict] = []
    provider = _FakeProvider(
        [
            _tool_turn("search", {"query": "q", "limit": 5}, "c1"),
            _tool_turn("search", {"limit": 5, "query": "q"}, "c2"),  # 키 순서만 다른 같은 인자
            _answer("done"),
        ]
    )

    _, history = await run_agent("q", [_search_tool(calls)], "sys", provider)

    assert len(calls) == 1
    second = next(m for m in history if m.tool_call_id == "c2")
    assert "이미 같은 도구를 같은 인자로 호출" in second.content


async def test_prefetched_search_blocks_the_model_repeating_the_question(monkeypatch):
    monkeypatch.setattr(settings, "agent_prefetch_search", True)
    calls: list[dict] = []
    provider = _FakeProvider([_tool_turn("search", {"query": "q"}), _answer("done")])

    await run_agent("q", [_search_tool(calls)], "sys", provider)

    assert calls == [{"query": "q"}]


async def test_failed_call_can_be_retried(monkeypatch):
    monkeypatch.setattr(settings, "agent_prefetch_search", False)
    attempts = []

    async def _flaky(args: dict) -> dict:
        attempts.append(args)
        if len(attempts) == 1:
            raise RuntimeError("boom")
        return {"ok": True}

    tool = ToolSpec(name="flaky", description="d", input_schema={"type": "object"}, handler=_flaky)
    provider = _FakeProvider([_tool_turn("flaky", {"a": 1}, "c1"), _tool_turn("flaky", {"a": 1}, "c2"), _answer("done")])

    await run_agent("q", [tool], "sys", provider)

    assert len(attempts) == 2


@pytest.mark.parametrize(
    "text",
    [
        '<tool_call>\n{"name": "search", "arguments": {"query": "x"}}\n</tool_call>',
        "<|channel|>commentary to=functions.search<|constrain|>json<|message|>{}<|call|>",
        '{"name": "search", "arguments": {"query": "x"}}',
    ],
)
async def test_tool_call_markup_in_final_text_raises(monkeypatch, text):
    monkeypatch.setattr(settings, "agent_prefetch_search", True)
    provider = _FakeProvider([_answer(text)])

    with pytest.raises(AgentError, match="도구 호출 파서"):
        await run_agent("q", [_search_tool()], "sys", provider)


async def test_answer_without_any_tool_call_is_sent_back_once(monkeypatch):
    monkeypatch.setattr(settings, "agent_prefetch_search", False)
    calls: list[dict] = []
    provider = _FakeProvider([_answer("guess"), _tool_turn("search", {"query": "q"}), _answer("grounded")])

    answer, _ = await run_agent("q", [_search_tool(calls)], "sys", provider)

    assert answer == "grounded"
    assert calls == [{"query": "q"}]
    assert "도구를 한 번도 호출하지 않고" in provider.seen_messages[1][-1].content


async def test_still_ungrounded_after_nudge_is_marked(monkeypatch):
    monkeypatch.setattr(settings, "agent_prefetch_search", False)
    provider = _FakeProvider([_answer("guess"), _answer("guess again")])

    answer, _ = await run_agent("q", [_search_tool()], "sys", provider)

    assert answer.startswith("[주의: 도구를 한 번도 호출하지 않고")
    assert answer.endswith("guess again")
