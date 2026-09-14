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
from typing import Any

import openai

from knowledge_forge.agent.auth import OPENAI_COMPATIBLE_AGENT_CREDENTIAL_HINT_KO
from knowledge_forge.agent.loop import INVALID_JSON_ARGUMENTS_KEY
from knowledge_forge.agent.providers.base import AssistantTurn, ChatProvider, Message, StopReason, ToolCall, ToolSpec
from knowledge_forge.config import settings
from knowledge_forge.exceptions import AgentError
from knowledge_forge.retry import RETRYABLE_OPENAI_ERRORS, openai_retry

DEFAULT_MODEL = "anthropic/claude-sonnet-5"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MAX_TOKENS = 4096


# No prompt-caching knob here, deliberately. The OpenAI Chat Completions
# schema has no cache primitive: OpenAI-hosted models cache prefixes
# automatically with nothing to declare, and OpenRouter only forwards
# Anthropic-style `cache_control` blocks when the request happens to be
# routed to an Anthropic model - which this class cannot assume, since the
# same code path also serves plain OpenAI and any other compatible endpoint
# (a local vLLM server) via `base_url`, where an unknown `cache_control` key
# inside a message is rejected. So the caching win
# (providers/anthropic.py's `_CACHE_BREAKPOINT`) is Anthropic-only.
def _to_openai_tools(tools: list[ToolSpec]) -> list[dict]:
    return [
        {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.input_schema}}
        for t in tools
    ]


def _parse_tool_call_arguments(raw: str | None) -> dict[str, Any]:
    """도구 호출 인자 JSON을 파싱한다 - 실패(또는 객체가 아닌 JSON)는 예외로 올려
    `provider.send()` 전체를, 나아가 `run_agent`를 죽이는 대신 `INVALID_JSON_ARGUMENTS_KEY`
    하나만 든 dict로 바꾼다. `agent/loop.py::_dispatch`가 핸들러를 부르기 전에 이 키를 보고
    평범한 도구 오류 결과(`error: invalid JSON arguments: ...`)로 돌려 모델이 다시 시도하게
    한다 - Qwen에서 관측된 깨진 JSON이 이 경로였다(`wiki/search-code-review-2026-09-14.md`
    결함11)."""
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        return {INVALID_JSON_ARGUMENTS_KEY: str(exc)}
    if not isinstance(parsed, dict):
        return {INVALID_JSON_ARGUMENTS_KEY: f"expected a JSON object, got {type(parsed).__name__}"}
    return parsed


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
            ToolCall(id=tc.id, name=tc.function.name, arguments=_parse_tool_call_arguments(tc.function.arguments))
            for tc in (msg.tool_calls or [])
        ]
        stop_reason: StopReason
        if tool_calls:
            stop_reason = "tool_use"
        elif choice.finish_reason == "length":
            stop_reason = "max_tokens"
        else:
            stop_reason = "end_turn"
        return AssistantTurn(text=msg.content, tool_calls=tool_calls, stop_reason=stop_reason)
