"""CLI-facing entry points for the agent runtime - assembles a provider
(agent/providers/factory.py), the Phase 1 tool set (agent/tools.py), and the
system prompt (agent/prompts.py) around agent/loop.py::run_agent().

`run_query` is the one-shot path; `AgentSession` is the `--interactive` REPL
path - both call the exact same `run_agent`, per wiki/agent-runtime.md's
"예외 없음" requirement. There is no separate "interactive backend".

Both also log each finished turn to `search_log` (wiki/search-evaluation.md
(A)) when `KF_DATABASE_URL` is configured - `run_agent` itself stays
logging-unaware (see `evaluation/search_log.py`'s docstring); this is the
"call site wraps it" layer that module's docstring refers to.
"""

from __future__ import annotations

import time

from knowledge_forge.agent.loop import run_agent
from knowledge_forge.agent.memory import read_memory
from knowledge_forge.agent.plan import PlanState
from knowledge_forge.agent.prompts import build_system_prompt
from knowledge_forge.agent.providers.base import Message
from knowledge_forge.agent.providers.factory import get_chat_provider
from knowledge_forge.agent.tools import default_tools
from knowledge_forge.config import settings
from knowledge_forge.evaluation.search_log import save_search_log


async def run_query(
    question: str,
    project_id: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> tuple[str, str | None]:
    """One-shot: ask a single question, get `(answer, log_id)` - no retained
    history. `log_id` is `None` when `KF_DATABASE_URL` isn't configured (or
    logging itself failed) - see `evaluation/search_log.py::save_search_log`."""
    chat_provider = get_chat_provider(provider=provider, model=model)
    system = build_system_prompt(project_id=project_id, memory=read_memory(project_id))
    tools = default_tools(project_id=project_id, provider=chat_provider, system=system)
    started_at = time.monotonic()
    answer, messages = await run_agent(
        question, tools, system, chat_provider, max_iterations=settings.agent_max_iterations
    )
    log_id = await _log_turn(question, answer, messages, started_at, project_id)
    return answer, log_id


async def _log_turn(
    question: str, answer: str, messages: list[Message], started_at: float, project_id: str | None
) -> str | None:
    if not settings.database_url:
        return None
    latency_ms = int((time.monotonic() - started_at) * 1000)
    return await save_search_log(question, answer, messages, latency_ms, project_id, settings.database_url)


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
        self._project_id = project_id
        self.last_log_id: str | None = None

    @property
    def plan(self) -> PlanState:
        """Current plan, as last written by the `write_plan` tool - empty
        until the agent decides a task warrants one. Exposed so callers
        (e.g. the `--interactive` CLI loop) can show it without reaching
        into `run_agent()`'s return value, which only ever carries the
        answer and message history."""
        return self._plan_state

    async def ask(self, question: str) -> str:
        started_at = time.monotonic()
        history_before = list(self._history)
        answer, self._history = await run_agent(
            question,
            self._tools,
            self._system,
            self._provider,
            max_iterations=settings.agent_max_iterations,
            history=self._history,
        )
        # Only this turn's new messages (not the whole accumulated REPL
        # history) go into the log - `history_before` is exactly the prefix
        # `run_agent` re-appends to (see its `history` in/out contract).
        turn_messages = self._history[len(history_before) :]
        self.last_log_id = await _log_turn(question, answer, turn_messages, started_at, self._project_id)
        return answer
