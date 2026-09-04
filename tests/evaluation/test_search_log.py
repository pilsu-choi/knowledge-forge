from knowledge_forge.agent.loop import _wrap_untrusted
from knowledge_forge.chat_provider import Message, ToolCall
from knowledge_forge.evaluation.search_log import extract_evidence, extract_tool_calls


def _turn_messages() -> list[Message]:
    """Mirrors the exact message shape `run_agent()` produces for one
    iteration that calls two tools, one of them not evidence-bearing."""
    return [
        Message(role="user", content="지난달 결정된 사항은?"),
        Message(
            role="assistant",
            content=None,
            tool_calls=[
                ToolCall(id="c1", name="file_search", arguments={"query": "결정"}),
                ToolCall(id="c2", name="write_memory", arguments={"content": "note"}),
            ],
        ),
        Message(role="tool", content=_wrap_untrusted("file_search", [{"path": "a.md", "score": 3}]), tool_call_id="c1"),
        Message(role="tool", content=_wrap_untrusted("write_memory", "메모리에 기록됨"), tool_call_id="c2"),
        Message(role="assistant", content="A 결정이 있었습니다."),
    ]


def test_extract_tool_calls_includes_every_call_with_iteration_number():
    calls = extract_tool_calls(_turn_messages())

    assert calls == [
        {"tool": "file_search", "arguments": {"query": "결정"}, "iteration": 1},
        {"tool": "write_memory", "arguments": {"content": "note"}, "iteration": 1},
    ]


def test_extract_evidence_only_keeps_evidence_tools():
    evidence = extract_evidence(_turn_messages())

    assert evidence == [{"tool": "file_search", "iteration": 1, "payload": [{"path": "a.md", "score": 3}]}]


def test_extract_evidence_tracks_iteration_across_multiple_rounds():
    messages = [
        Message(role="user", content="q"),
        Message(
            role="assistant", content=None, tool_calls=[ToolCall(id="c1", name="file_search", arguments={"query": "a"})]
        ),
        Message(role="tool", content=_wrap_untrusted("file_search", [{"path": "1.md"}]), tool_call_id="c1"),
        Message(
            role="assistant", content=None, tool_calls=[ToolCall(id="c2", name="vector_search", arguments={"query": "b"})]
        ),
        Message(role="tool", content=_wrap_untrusted("vector_search", [{"path": "2.md"}]), tool_call_id="c2"),
        Message(role="assistant", content="answer"),
    ]

    evidence = extract_evidence(messages)

    assert [e["iteration"] for e in evidence] == [1, 2]


def test_extract_evidence_ignores_plain_text_tool_content():
    messages = [
        Message(role="user", content="q"),
        Message(role="tool", content="not a tool_result envelope", tool_call_id="orphan"),
    ]

    assert extract_evidence(messages) == []
