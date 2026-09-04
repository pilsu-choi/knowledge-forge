from knowledge_forge.agent.compaction import _TOKEN_CACHE, compact_messages, count_messages_tokens
from knowledge_forge.agent.providers.base import AssistantTurn, ChatProvider, Message, ToolCall


class _FakeSummarizer(ChatProvider):
    """Records every `send()` call and always answers with a fixed summary -
    stands in for the SAME provider `run_agent` reuses for summarization
    (agent/compaction.py's whole point is not needing a separate one)."""

    def __init__(self, summary_text: str = "요약된 내용"):
        self._summary_text = summary_text
        self.calls: list[tuple[list[Message], list, str]] = []

    async def send(self, messages, tools, system) -> AssistantTurn:
        self.calls.append((messages, tools, system))
        return AssistantTurn(text=self._summary_text, tool_calls=[], stop_reason="end_turn")


def _round_trip(i: int, text: str) -> list[Message]:
    """One assistant(tool_calls) + tool(result) pair - the atomic unit a
    compaction boundary must never split, mirroring how agent/loop.py
    actually appends messages."""
    return [
        Message(
            role="assistant",
            content=None,
            tool_calls=[ToolCall(id=f"c{i}", name="file_search", arguments={"query": f"q{i}"})],
        ),
        Message(role="tool", content=text, tool_call_id=f"c{i}"),
    ]


async def test_returns_messages_unchanged_when_under_budget():
    messages = [Message(role="user", content="hello")] + _round_trip(1, "short result")
    provider = _FakeSummarizer()

    result = await compact_messages(messages, provider, max_tokens=100_000, protect_tail=8)

    assert result is messages
    assert provider.calls == []


async def test_compacts_middle_round_trips_into_one_summary_message():
    # First user question (protected head) + 6 round trips (12 messages) of
    # large content, well over a tiny token budget.
    big = "x " * 2000
    messages = [Message(role="user", content="question")]
    for i in range(6):
        messages += _round_trip(i, big)

    provider = _FakeSummarizer("[요약]")
    result = await compact_messages(messages, provider, max_tokens=50, protect_tail=4, summary_target_tokens=10)

    assert len(provider.calls) == 1  # exactly one summarization call
    # Head preserved verbatim.
    assert result[0] == messages[0]
    # Tail (last `protect_tail` messages) preserved verbatim.
    assert result[-4:] == messages[-4:]
    # head(1) + summary(1) + protected tail(4) - the whole compressible
    # middle collapsed into one message.
    assert len(result) == 1 + 1 + 4
    summary_message = result[1]
    assert summary_message.role == "user"
    assert summary_message.content is not None
    assert "[컨텍스트 압축 요약]" in summary_message.content
    assert "[요약]" in summary_message.content


async def test_never_splits_a_tool_calls_message_from_its_tool_result():
    big = "x " * 2000
    messages = [Message(role="user", content="question")]
    for i in range(6):
        messages += _round_trip(i, big)

    provider = _FakeSummarizer()
    result = await compact_messages(messages, provider, max_tokens=50, protect_tail=4, summary_target_tokens=10)

    for i, m in enumerate(result):
        if m.role == "tool":
            assert result[i - 1].role == "assistant" and result[i - 1].tool_calls


async def test_leaves_messages_unchanged_when_no_safe_region_to_compress():
    # Only a head + a couple of round trips that entirely fall inside the
    # protected tail - nothing left to compress, even though we're over
    # budget (one giant message).
    messages = [Message(role="user", content="q")] + _round_trip(1, "x " * 5000)
    provider = _FakeSummarizer()

    result = await compact_messages(messages, provider, max_tokens=10, protect_tail=8)

    assert result is messages
    assert provider.calls == []


async def test_summarization_failure_falls_back_to_a_placeholder_instead_of_raising():
    class _BoomProvider(ChatProvider):
        async def send(self, messages, tools, system):
            raise RuntimeError("boom")

    big = "x " * 2000
    messages = [Message(role="user", content="question")]
    for i in range(6):
        messages += _round_trip(i, big)

    result = await compact_messages(messages, _BoomProvider(), max_tokens=50, protect_tail=4, summary_target_tokens=10)

    summary_message = result[1]
    assert "압축되었다" in summary_message.content


def test_count_messages_tokens_counts_tool_call_arguments_too():
    empty = Message(role="assistant", content=None, tool_calls=[])
    with_call = Message(
        role="assistant", content=None, tool_calls=[ToolCall(id="c1", name="t", arguments={"query": "hello world"})]
    )

    assert count_messages_tokens([with_call]) > count_messages_tokens([empty])


async def test_token_counts_are_cached_so_a_growing_transcript_is_not_retokenized(monkeypatch):
    """`run_agent` calls `compact_messages` once per loop iteration on a
    transcript that only grows at the tail - counting every message every
    time made compaction quadratic in transcript length. Only the messages
    that are new since the last call may reach the tokenizer."""
    counted: list[str] = []

    def _spy(text: str) -> int:
        counted.append(text)
        return len(text)

    monkeypatch.setattr("knowledge_forge.agent.compaction.count_tokens", _spy)
    _TOKEN_CACHE.clear()

    messages = [Message(role="user", content="첫 질문")] + _round_trip(1, "결과 하나")
    provider = _FakeSummarizer()
    await compact_messages(messages, provider, max_tokens=100_000, protect_tail=8)
    first_pass = len(counted)
    assert first_pass == len(messages)

    messages = messages + _round_trip(2, "결과 둘")
    await compact_messages(messages, provider, max_tokens=100_000, protect_tail=8)

    assert len(counted) - first_pass == 2  # only the two newly appended messages
