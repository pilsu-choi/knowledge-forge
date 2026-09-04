"""Reconstructs a `search_log` row's `evidence`/`tool_calls` fields from a
finished `run_agent()` turn's `messages` (wiki/search-evaluation.md (A)).

Deliberately lives outside `agent/loop.py`: the shared harness loop must not
know logging exists (the same reasoning `agent/loop.py::run_agent`'s own
docstring gives for staying vendor/mode-neutral) - so this parses what
`run_agent` already produced (via `parse_tool_result`) instead of the loop
calling out to it. Callers are `agent/runtime.py::run_query`/`AgentSession.ask`,
once per finished turn, never `run_agent` itself.
"""

from __future__ import annotations

import logging
from typing import Any

from knowledge_forge.agent.loop import parse_tool_result
from knowledge_forge.chat_provider import Message

logger = logging.getLogger(__name__)

# Tools whose result is retrieved evidence, as opposed to a filesystem/
# memory/plan side effect (read_file, write_memory, write_plan, ...) - only
# these are worth keeping for the eval judge (search-evaluation.md (B)) to
# check the answer's claims against.
EVIDENCE_TOOLS = {"file_search", "vector_search", "sql_search", "plan_search"}


def extract_tool_calls(messages: list[Message]) -> list[dict[str, Any]]:
    """`[{tool, arguments, iteration}, ...]` for every tool call across the
    turn, in call order - `iteration` is the 1-based tool-call round trip it
    happened in (one `run_agent()` loop pass can dispatch several calls at
    once, so more than one entry can share an `iteration`)."""
    calls: list[dict[str, Any]] = []
    iteration = 0
    for message in messages:
        if message.role != "assistant" or not message.tool_calls:
            continue
        iteration += 1
        for call in message.tool_calls:
            calls.append({"tool": call.name, "arguments": call.arguments, "iteration": iteration})
    return calls


def extract_evidence(messages: list[Message]) -> list[dict[str, Any]]:
    """`[{tool, iteration, payload}, ...]` - one entry per `EVIDENCE_TOOLS`
    result, `payload` decoded via `parse_tool_result` and kept in that
    tool's own native shape (see schema.sql's `search_log.evidence` comment
    for why this isn't normalized further)."""
    evidence: list[dict[str, Any]] = []
    iteration = 0
    for message in messages:
        if message.role == "assistant" and message.tool_calls:
            iteration += 1
            continue
        if message.role != "tool" or message.content is None:
            continue
        parsed = parse_tool_result(message.content)
        if parsed is None:
            continue
        tool_name, payload = parsed
        if tool_name not in EVIDENCE_TOOLS:
            continue
        evidence.append({"tool": tool_name, "iteration": iteration, "payload": payload})
    return evidence


async def save_search_log(
    question: str,
    answer: str,
    messages: list[Message],
    latency_ms: int,
    project_id: str | None,
    database_url: str,
) -> str | None:
    """Best-effort: opens its own `EvalStore` connection (same "connect per
    call" convention as `agent/tools.py`'s `vector_search`/`sql_search`
    handlers) and writes one `search_log` row, returning its `log_id` for
    `knowledge-forge feedback`/the `--interactive` `/feedback` command to
    attach a rating to later. Never raises - a turn's answer is already
    produced by the time this runs, so a logging failure (DB down, schema
    not migrated yet) must not take that answer down with it; it's logged
    and swallowed instead."""
    from knowledge_forge.storage.eval_store import EvalStore

    try:
        async with EvalStore(database_url) as store:
            log = await store.log_search(
                query=question,
                answer=answer,
                evidence=extract_evidence(messages),
                tool_calls=extract_tool_calls(messages),
                latency_ms=latency_ms,
                project_id=project_id or "default",
            )
        return log.log_id
    except Exception as exc:  # noqa: BLE001 - logging must never take an already-produced answer down with it
        logger.warning("search_log 기록 실패 (질문에 대한 답변 자체는 정상 반환됨): %s", exc)
        return None
