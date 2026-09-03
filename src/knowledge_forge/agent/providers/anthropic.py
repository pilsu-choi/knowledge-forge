"""Anthropic Messages API adapter - translates the harness's internal
`Message`/`ToolCall` shape to/from Anthropic's wire format. Modeled on
`extraction/claude_extractor.py`'s client construction/retry/error-wrapping
pattern, but `send()` is stateless per call (see providers/base.py) rather
than extraction's one-shot `extract()`.
"""

from __future__ import annotations

import asyncio

import anthropic

from knowledge_forge.agent.auth import ANTHROPIC_AGENT_CREDENTIAL_HINT_KO
from knowledge_forge.agent.providers.base import AssistantTurn, ChatProvider, Message, ToolCall, ToolSpec
from knowledge_forge.config import settings
from knowledge_forge.exceptions import AgentError
from knowledge_forge.retry import RETRYABLE_ANTHROPIC_ERRORS, anthropic_retry

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_MAX_TOKENS = 4096


def _to_anthropic_tools(tools: list[ToolSpec]) -> list[dict]:
    return [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools]


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
    ):
        # A bare AsyncAnthropic() resolves credentials itself (API key, OAuth
        # profile, ...) - same as ClaudeExtractor, see its docstring.
        self._client = client or anthropic.AsyncAnthropic()
        self._model = model
        self._max_tokens = max_tokens
        self._semaphore = asyncio.Semaphore(max_concurrency or settings.max_concurrent_llm_requests)

    @anthropic_retry()
    async def send(self, messages: list[Message], tools: list[ToolSpec], system: str) -> AssistantTurn:
        kwargs: dict = dict(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            messages=_to_anthropic_messages(messages),
        )
        if tools:
            kwargs["tools"] = _to_anthropic_tools(tools)

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

        text_parts = [block.text for block in response.content if block.type == "text"]
        tool_calls = [
            ToolCall(id=block.id, name=block.name, arguments=block.input)
            for block in response.content
            if block.type == "tool_use"
        ]
        if tool_calls:
            stop_reason = "tool_use"
        elif response.stop_reason == "max_tokens":
            stop_reason = "max_tokens"
        else:
            stop_reason = "end_turn"
        return AssistantTurn(text="\n".join(text_parts) or None, tool_calls=tool_calls, stop_reason=stop_reason)
