"""OpenAI-compatible chat-completions adapter - covers OpenRouter (default),
plain OpenAI, and any other OpenAI-Chat-Completions-compatible endpoint via
`base_url` (a local vLLM server, etc.). Modeled on
`extraction/openrouter_extractor.py`'s client construction/retry/
error-wrapping pattern; `send()` is stateless per call (see
providers/base.py) rather than extraction's one-shot `extract()`.
"""

from __future__ import annotations

import asyncio
import json
import os

import openai

from knowledge_forge.agent.auth import OPENAI_COMPATIBLE_AGENT_CREDENTIAL_HINT_KO
from knowledge_forge.agent.providers.base import AssistantTurn, ChatProvider, Message, ToolCall, ToolSpec
from knowledge_forge.config import settings
from knowledge_forge.exceptions import AgentError
from knowledge_forge.retry import RETRYABLE_OPENAI_ERRORS, openai_retry

DEFAULT_MODEL = "anthropic/claude-sonnet-5"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MAX_TOKENS = 4096


def _to_openai_tools(tools: list[ToolSpec]) -> list[dict]:
    return [
        {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.input_schema}}
        for t in tools
    ]


def _to_openai_messages(messages: list[Message], system: str) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        if m.role == "tool":
            out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content or ""})
            continue
        if m.role == "assistant":
            entry: dict = {"role": "assistant", "content": m.content}
            if m.tool_calls:
                entry["tool_calls"] = [
                    {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                    for tc in m.tool_calls
                ]
            out.append(entry)
            continue
        out.append({"role": "user", "content": m.content or ""})
    return out


class OpenAICompatibleChatProvider(ChatProvider):
    def __init__(
        self,
        client: openai.AsyncOpenAI | None = None,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        max_concurrency: int | None = None,
    ):
        # openai.AsyncOpenAI()'s zero-arg credential resolution only looks
        # for OPENAI_API_KEY, not OPENROUTER_API_KEY - same fallback-to-
        # placeholder-and-let-the-401-surface approach as
        # OpenRouterExtractor, for the same reason (keeps construction cheap
        # and lazy instead of raising immediately with no key).
        self._client = client or openai.AsyncOpenAI(
            api_key=api_key or os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY") or "unset",
            base_url=base_url,
        )
        self._model = model
        self._max_tokens = max_tokens
        self._semaphore = asyncio.Semaphore(max_concurrency or settings.max_concurrent_llm_requests)

    @openai_retry()
    async def send(self, messages: list[Message], tools: list[ToolSpec], system: str) -> AssistantTurn:
        kwargs: dict = dict(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=_to_openai_messages(messages, system),
        )
        if tools:
            kwargs["tools"] = _to_openai_tools(tools)

        async with self._semaphore:
            try:
                response = await self._client.chat.completions.create(**kwargs)
            except RETRYABLE_OPENAI_ERRORS:
                raise
            except openai.APIError as exc:
                message = f"agent chat call failed (openai-compatible): {exc}"
                if isinstance(exc, openai.AuthenticationError):
                    message = OPENAI_COMPATIBLE_AGENT_CREDENTIAL_HINT_KO
                raise AgentError(message) from exc

        choice = response.choices[0]
        msg = choice.message
        tool_calls = [
            ToolCall(id=tc.id, name=tc.function.name, arguments=json.loads(tc.function.arguments or "{}"))
            for tc in (msg.tool_calls or [])
        ]
        if tool_calls:
            stop_reason = "tool_use"
        elif choice.finish_reason == "length":
            stop_reason = "max_tokens"
        else:
            stop_reason = "end_turn"
        return AssistantTurn(text=msg.content, tool_calls=tool_calls, stop_reason=stop_reason)
