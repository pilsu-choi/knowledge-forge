"""Vendor-neutral chat primitives for the agent runtime (harness) -
wiki/agent-runtime.md's "왜 자체 ChatProvider 추상화 위에 짓는가".

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
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal


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


@dataclass
class AssistantTurn:
    text: str | None
    tool_calls: list[ToolCall]
    stop_reason: Literal["tool_use", "end_turn", "max_tokens"]


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
