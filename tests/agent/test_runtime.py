from knowledge_forge.agent.providers.base import AssistantTurn, ChatProvider, Message, ToolCall
from knowledge_forge.agent.runtime import AgentSession


class _FakeProvider(ChatProvider):
    def __init__(self, turns: list[AssistantTurn]):
        self._turns = list(turns)

    async def send(self, messages: list[Message], tools, system) -> AssistantTurn:
        return self._turns.pop(0)


async def test_session_plan_starts_empty(monkeypatch):
    monkeypatch.setattr(
        "knowledge_forge.agent.runtime.get_chat_provider",
        lambda provider, model: _FakeProvider([AssistantTurn(text="hi", tool_calls=[], stop_reason="end_turn")]),
    )

    session = AgentSession()

    assert session.plan.steps == []


async def test_session_plan_persists_across_turns(monkeypatch):
    monkeypatch.setattr(
        "knowledge_forge.agent.runtime.get_chat_provider",
        lambda provider, model: _FakeProvider(
            [
                AssistantTurn(
                    text=None,
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="write_plan",
                            arguments={"steps": [{"content": "search project A", "status": "in_progress"}]},
                        )
                    ],
                    stop_reason="tool_use",
                ),
                AssistantTurn(text="working on it", tool_calls=[], stop_reason="end_turn"),
                AssistantTurn(text="second turn sees the same plan", tool_calls=[], stop_reason="end_turn"),
            ]
        ),
    )

    session = AgentSession()

    await session.ask("do a multi-step task")
    assert [s.content for s in session.plan.steps] == ["search project A"]

    await session.ask("continue")
    # The plan set during the first turn is still there on the second - it
    # is not reset per `run_agent()` call the way a fresh `default_tools()`
    # call (e.g. in `run_query`) would reset it.
    assert [s.content for s in session.plan.steps] == ["search project A"]
