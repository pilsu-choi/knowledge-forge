import pytest

from knowledge_forge.agent.prompts import build_system_prompt
from knowledge_forge.agent.providers.base import AssistantTurn, ChatProvider, Message, ToolCall
from knowledge_forge.agent.runtime import AgentSession
from knowledge_forge.config import settings

_EXPECTED_NON_SEARCH_TOOL_NAMES = [
    "read_file",
    "write_file",
    "edit_file",
    "list_dir",
    "write_memory",
    "write_plan",
    "delegate_task",
]


@pytest.fixture(autouse=True)
def _default_tool_surface(monkeypatch):
    """개발자 로컬 `.env`의 `KF_AGENT_TOOL_SURFACE`에 이 파일의 단언이 끌려가지
    않게 기본값에 고정한다(tests/agent/test_tools.py와 같은 이유)."""
    monkeypatch.setattr(settings, "agent_tool_surface", "minimal")


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


def test_session_wires_up_the_default_search_tool_surface(monkeypatch):
    """`AgentSession`이 실제로 들고 도는 도구 목록 - `default_tools()` 단위
    테스트와 별개로, CLI가 쓰는 조립 경로에서도 검색 도구가 `search` 하나 +
    비상구 둘로 유지되는지 본다."""
    monkeypatch.setattr(settings, "database_url", "postgresql://kf:kf@localhost/kf")
    monkeypatch.setattr(
        "knowledge_forge.agent.runtime.get_chat_provider",
        lambda provider, model: _FakeProvider([]),
    )

    session = AgentSession()

    assert [t.name for t in session._tools] == [
        "search",
        "read_document",
        "sql_search",
    ] + _EXPECTED_NON_SEARCH_TOOL_NAMES


def test_system_prompt_leads_with_search_and_matches_the_tool_surface(monkeypatch):
    """프롬프트의 1원칙은 도구 목록의 1순위 도구와 같아야 한다 - 예전엔 프롬프트가
    `file_search`를 먼저 부르라고 시키고 있었는데, 그건 골든셋이 재는 경로가 아니고
    `KF_OKF_DIR`이 없으면 조용히 `[]`만 돌려주는 경로였다. 그리고 목록에 없는 도구의
    라우팅 지침은 프롬프트에도 없어야 한다."""
    first_principle = build_system_prompt().split("원칙:\n")[1].splitlines()[0]

    assert first_principle.startswith("- 답하기 전 최소 1회 search(통합 검색)")

    minimal = build_system_prompt()
    assert "document_search" not in minimal
    assert "lexical_search" not in minimal
    assert "plan_search" not in minimal

    # `file_search`도 같은 규칙을 탄다 - 기본값(`KF_AGENT_FILE_SEARCH_ENABLED=false`)에서는
    # 도구 목록에 없으므로 "그 구성에서 1순위로 쓸 것"이라는 지침도 없어야 한다.
    monkeypatch.setattr(settings, "agent_file_search_enabled", False)
    assert "file_search" not in build_system_prompt()
    monkeypatch.setattr(settings, "agent_file_search_enabled", True)
    assert "file_search" in build_system_prompt()
    monkeypatch.setattr(settings, "agent_file_search_enabled", False)

    monkeypatch.setattr(settings, "agent_tool_surface", "full")
    full = build_system_prompt()
    assert "document_search" in full and "plan_search" in full
    # "full"에서도 개별 도구는 보충 수단으로만 서술된다 - 동급 메뉴로 늘어놓는 순간
    # 모델이 다시 스스로 라우팅한다.
    assert full.split("원칙:\n")[1].splitlines()[0] == first_principle
