"""CLI-facing entry points for the agent runtime - assembles a provider
(agent/providers/factory.py), the Phase 1 tool set (agent/tools.py), and the
system prompt (agent/prompts.py) around agent/loop.py::run_agent().

`run_query` is the one-shot path; `AgentSession` is the `--interactive` REPL
path - both call the exact same `run_agent`, per wiki/agent-runtime.md's
"예외 없음" requirement. There is no separate "interactive backend".
"""

from __future__ import annotations

from knowledge_forge.agent.loop import run_agent
from knowledge_forge.agent.memory import read_memory
from knowledge_forge.agent.plan import PlanState
from knowledge_forge.agent.prompts import build_system_prompt
from knowledge_forge.agent.providers.base import Message
from knowledge_forge.agent.providers.factory import get_chat_provider
from knowledge_forge.agent.tools import default_tools
from knowledge_forge.config import settings


async def run_query(
    question: str,
    project_id: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> str:
    """One-shot: ask a single question, get the answer - no retained history."""
    chat_provider = get_chat_provider(provider=provider, model=model)
    system = build_system_prompt(project_id=project_id, memory=read_memory(project_id))
    tools = default_tools(project_id=project_id, provider=chat_provider, system=system)
    answer, _ = await run_agent(question, tools, system, chat_provider, max_iterations=settings.agent_max_iterations)
    return answer


class AgentSession:
    """`--interactive` REPL state - re-calls `run_agent` once per user turn,
    threading `history` through so the conversation actually accumulates
    (see agent/loop.py::run_agent's `history` in/out parameter)."""

    def __init__(
        self,
        project_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
    ):
        self._provider = get_chat_provider(provider=provider, model=model)
        self._plan_state = PlanState()
        self._system = build_system_prompt(project_id=project_id, memory=read_memory(project_id))
        self._tools = default_tools(
            plan_state=self._plan_state, project_id=project_id, provider=self._provider, system=self._system
        )
        self._history: list[Message] = []

    @property
    def plan(self) -> PlanState:
        """Current plan, as last written by the `write_plan` tool - empty
        until the agent decides a task warrants one. Exposed so callers
        (e.g. the `--interactive` CLI loop) can show it without reaching
        into `run_agent()`'s return value, which only ever carries the
        answer and message history."""
        return self._plan_state

    async def ask(self, question: str) -> str:
        answer, self._history = await run_agent(
            question,
            self._tools,
            self._system,
            self._provider,
            max_iterations=settings.agent_max_iterations,
            history=self._history,
        )
        return answer
