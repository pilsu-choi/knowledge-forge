"""Vendor-neutral chat primitives - wiki/agent-runtime.md's "왜 자체
ChatProvider 추상화 위에 짓는가". Lives directly under `knowledge_forge/`
(a sibling of `agent/`/`search/`), not inside `agent/providers/`, precisely
so `search/planner.py` can call an LLM (for query decomposition) without
importing the `agent` package - `agent/__init__.py` eagerly imports
`agent.runtime` -> `agent.tools` -> `search.planner`, so `search/planner.py`
importing anything from `agent.*` at module scope is a circular import
(and, independent of the crash, a layering violation: `search/` is meant to
sit below `agent/`, `agent/tools.py` wraps `search/*` functions, never the
other way around - wiki/agent-runtime.md's "계층 설계"). `agent/providers/base.py`
re-exports everything here unchanged so no existing `agent/*` import needs
to change.

`Message`/`ToolCall` are OUR internal representation, not either vendor's
wire format - `agent/loop.py` only ever appends these, and each
`ChatProvider.send()` translates the *entire* history to that provider's
API shape from scratch on every call (stateless per call, so there is
nothing provider-specific for the loop to carry between turns). This is
also why `send()` takes the whole `messages` list rather than "the next
message" - both Anthropic and OpenAI-compatible APIs are themselves
stateless per request; the conversation state lives in `agent/loop.py`,
not in either SDK client.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Message:
    role: Literal["user", "assistant", "tool"]
    content: str | None = None
    # Assistant-only: the tool calls this turn asked for (empty when the
    # turn was a plain text reply).
    tool_calls: list[ToolCall] = field(default_factory=list)
    # Tool-only: which call this result answers - Anthropic needs this to
    # rebuild `tool_use_id`, OpenAI-compatible needs it for `tool_call_id`.
    tool_call_id: str | None = None
    # Assistant-only: the model's thinking text (OpenAI-compatible
    # `reasoning_content`/`reasoning`). Kept so it can be echoed back on the
    # next request - a thinking model that calls tools expects its own chain
    # of thought in the history.
    reasoning: str | None = None


StopReason = Literal["tool_use", "end_turn", "max_tokens"]


@dataclass
class AssistantTurn:
    text: str | None
    tool_calls: list[ToolCall]
    stop_reason: StopReason
    reasoning: str | None = None
    # Seconds from request start to the first streamed text token; only set
    # by `send_stream` (None for plain `send`, or when no text was streamed).
    ttft_s: float | None = None


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Awaitable[Any]]


class ChatProvider(ABC):
    """One stateless "send the whole conversation, get one assistant turn
    back" primitive. Implementations own wire-format translation only - the
    tool-call loop (deciding whether to continue, dispatching tools,
    appending results) lives once in `agent/loop.py` and is identical for
    every provider."""

    @abstractmethod
    async def send(self, messages: list[Message], tools: list[ToolSpec], system: str) -> AssistantTurn: ...

    async def send_stream(
        self, messages: list[Message], tools: list[ToolSpec], system: str, on_text: Callable[[str], None]
    ) -> AssistantTurn:
        """Same as `send`, but calls `on_text(delta)` for each text fragment as
        it arrives. The default has no streaming: it sends normally and emits
        the whole text once, so a provider that can't stream (or a test fake)
        still satisfies the interface. Text that precedes a tool call is
        streamed too - the loop can't know a turn is final until it ends."""
        turn = await self.send(messages, tools, system)
        if turn.text:
            on_text(turn.text)
        return turn
