"""Anthropic Messages API adapter - translates the harness's internal
`Message`/`ToolCall` shape to/from Anthropic's wire format. Modeled on
`extraction/claude_extractor.py`'s client construction/retry/error-wrapping
pattern, but `send()` is stateless per call (see providers/base.py) rather
than extraction's one-shot `extract()`.

Prompt caching lives here rather than in the loop because the cache is a
wire-format concern (`cache_control` blocks) and because `agent/loop.py` is
provider-neutral by design - openai_compatible.py has no equivalent it can
turn on unconditionally (see that module).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

import anthropic

from knowledge_forge.agent.auth import ANTHROPIC_AGENT_CREDENTIAL_HINT_KO
from knowledge_forge.agent.providers.base import AssistantTurn, ChatProvider, Message, StopReason, ToolCall, ToolSpec
from knowledge_forge.config import settings
from knowledge_forge.exceptions import AgentError
from knowledge_forge.retry import RETRYABLE_ANTHROPIC_ERRORS, anthropic_retry

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_MAX_TOKENS = 4096

logger = logging.getLogger(__name__)


# Anthropic prompt-caching breakpoint (5-minute TTL, the default). One
# `run_agent` turn makes up to `agent_max_iterations` (8) calls that all
# resend the same tools + system prompt, and an `--interactive` session
# keeps doing that turn after turn, so this prefix is by far the most
# reread part of every request. Cache reads bill at ~0.1x the input rate
# and a 5-minute-TTL write at 1.25x, so two calls sharing a prefix already
# pay for themselves - and agent iterations are seconds apart, with each
# read refreshing the entry's timer, so the default TTL keeps the entry
# warm for a whole session (a 1h TTL would only double the write price).
_CACHE_BREAKPOINT = {"type": "ephemeral"}


def _to_anthropic_tools(tools: list[ToolSpec]) -> list[dict]:
    """The tools block, with a cache breakpoint on the last definition.

    Caching is a PREFIX match and the render order is tools -> system ->
    messages, so this breakpoint caches exactly the tool schemas, and the
    one on `system` below caches tools+system together. Two breakpoints
    (of the 4 allowed per request) rather than one because the two prefixes
    have different lifetimes: the tool set is identical for every session on
    this machine, while `system` carries a per-project hint and the prior
    session's memory (agent/prompts.py::build_system_prompt), so a different
    project still reads the tools half of the cache. Callers must keep the
    tool list in a stable order - reordering it invalidates everything."""
    out = [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools]
    if out:
        out[-1] = {**out[-1], "cache_control": _CACHE_BREAKPOINT}
    return out


def _to_anthropic_system(system: str) -> list[dict]:
    """The system prompt as one cached text block. Nothing volatile may be
    interpolated into it (no timestamps, no per-request ids) or the prefix
    changes every call and the cache never reads - agent/prompts.py builds
    it once per session from a static template, which is what makes this
    work. A prefix under the model's minimum cacheable length simply isn't
    cached: no error, `cache_creation_input_tokens` stays 0."""
    return [{"type": "text", "text": system, "cache_control": _CACHE_BREAKPOINT}]


def _to_anthropic_messages(messages: list[Message]) -> list[dict]:
    """Anthropic has no `role=tool` - a tool result is a `role=user` message
    with `tool_result` content blocks, and the API requires every
    `tool_use` block an assistant turn emitted to be answered in ONE
    following user message (not split across several) - so consecutive
    internal `tool` messages (one per call the loop dispatched) are
    coalesced here into a single user message with multiple content
    blocks."""
    out: list[dict] = []
    i = 0
    while i < len(messages):
        m = messages[i]
        if m.role == "tool":
            blocks = []
            while i < len(messages) and messages[i].role == "tool":
                blocks.append(
                    {"type": "tool_result", "tool_use_id": messages[i].tool_call_id, "content": messages[i].content or ""}
                )
                i += 1
            out.append({"role": "user", "content": blocks})
            continue
        if m.role == "assistant":
            content: list[dict] = []
            if m.content:
                content.append({"type": "text", "text": m.content})
            for tc in m.tool_calls:
                content.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments})
            out.append({"role": "assistant", "content": content})
            i += 1
            continue
        out.append({"role": "user", "content": m.content or ""})
        i += 1
    return out


class AnthropicChatProvider(ChatProvider):
    def __init__(
        self,
        client: anthropic.AsyncAnthropic | None = None,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        max_concurrency: int | None = None,
        temperature: float | None = None,
    ):
        # A bare AsyncAnthropic() resolves credentials itself (API key, OAuth
        # profile, ...) - same as ClaudeExtractor, see its docstring.
        self._client = client or anthropic.AsyncAnthropic(timeout=settings.agent_request_timeout_s)
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._semaphore = asyncio.Semaphore(max_concurrency or settings.max_concurrent_llm_requests)

    def _request_kwargs(self, messages: list[Message], tools: list[ToolSpec], system: str) -> dict:
        kwargs: dict = dict(
            model=self._model,
            max_tokens=self._max_tokens,
            system=_to_anthropic_system(system),
            messages=_to_anthropic_messages(messages),
        )
        if tools:
            kwargs["tools"] = _to_anthropic_tools(tools)
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature
        return kwargs

    @anthropic_retry()
    async def send(self, messages: list[Message], tools: list[ToolSpec], system: str) -> AssistantTurn:
        kwargs = self._request_kwargs(messages, tools, system)
        async with self._semaphore:
            try:
                response = await self._client.messages.create(**kwargs)
            except RETRYABLE_ANTHROPIC_ERRORS:
                raise
            except anthropic.APIError as exc:
                raise AgentError(f"agent chat call failed (anthropic): {exc}") from exc
            except TypeError as exc:
                # Same bare-TypeError-on-no-credential quirk documented in
                # claude_extractor.py's `_call_with_retry`.
                if "Could not resolve authentication" in str(exc):
                    raise AgentError(ANTHROPIC_AGENT_CREDENTIAL_HINT_KO) from exc
                raise
        return _to_turn(response)

    @anthropic_retry()
    async def send_stream(
        self, messages: list[Message], tools: list[ToolSpec], system: str, on_text: Callable[[str], None]
    ) -> AssistantTurn:
        kwargs = self._request_kwargs(messages, tools, system)
        started = time.perf_counter()
        ttft_s = None
        async with self._semaphore:
            try:
                async with self._client.messages.stream(**kwargs) as stream:
                    async for text in stream.text_stream:
                        if ttft_s is None:
                            ttft_s = time.perf_counter() - started
                            logger.info("agent stream TTFT %.3fs (model=%s)", ttft_s, self._model)
                        on_text(text)
                    response = await stream.get_final_message()
            except RETRYABLE_ANTHROPIC_ERRORS:
                raise
            except anthropic.APIError as exc:
                raise AgentError(f"agent chat call failed (anthropic): {exc}") from exc
            except TypeError as exc:
                if "Could not resolve authentication" in str(exc):
                    raise AgentError(ANTHROPIC_AGENT_CREDENTIAL_HINT_KO) from exc
                raise
        turn = _to_turn(response)
        turn.ttft_s = ttft_s
        return turn


def _to_turn(response) -> AssistantTurn:
    text_parts = [block.text for block in response.content if block.type == "text"]
    tool_calls = [
        ToolCall(id=block.id, name=block.name, arguments=block.input)
        for block in response.content
        if block.type == "tool_use"
    ]
    stop_reason: StopReason
    if tool_calls:
        stop_reason = "tool_use"
    elif response.stop_reason == "max_tokens":
        stop_reason = "max_tokens"
    else:
        stop_reason = "end_turn"
    return AssistantTurn(text="\n".join(text_parts) or None, tool_calls=tool_calls, stop_reason=stop_reason)
