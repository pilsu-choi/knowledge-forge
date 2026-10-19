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
import logging
import os
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import openai

from knowledge_forge.agent.auth import OPENAI_COMPATIBLE_AGENT_CREDENTIAL_HINT_KO, is_local_base_url
from knowledge_forge.agent.loop import INVALID_JSON_ARGUMENTS_KEY
from knowledge_forge.agent.providers.base import AssistantTurn, ChatProvider, Message, StopReason, ToolCall, ToolSpec
from knowledge_forge.config import settings
from knowledge_forge.exceptions import AgentError
from knowledge_forge.retry import RETRYABLE_OPENAI_ERRORS, openai_retry

DEFAULT_MODEL = "anthropic/claude-sonnet-5"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MAX_TOKENS = 4096

logger = logging.getLogger(__name__)


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


def _reasoning_extra_body(base_url: str, enabled: bool) -> dict[str, Any]:
    """thinking 켜기/끄기 요청 파라미터 - 서빙마다 이름이 달라 base_url로 고른다.
    OpenRouter는 통합 `reasoning` 객체, 그 밖의 OpenAI 호환 서버(vLLM/SGLang의 Qwen 등)는 챗
    템플릿 인자 `chat_template_kwargs.enable_thinking`이다. 둘을 항상 같이 보내지 않는 이유:
    api.openai.com 같은 엄격한 서버는 모르는 파라미터를 400으로 거부하므로 그쪽은 아무것도
    보내지 않는다."""
    host = urlparse(base_url).hostname or ""
    if host.endswith("openrouter.ai"):
        return {"reasoning": {"enabled": enabled}}
    if host.endswith("openai.com"):
        return {}
    return {"chat_template_kwargs": {"enable_thinking": enabled}}


_TRUNCATED_ARGUMENTS_REASON = (
    "응답이 max_tokens 한도(finish_reason=length)에서 잘려 도구 호출 인자가 끊겼다 - "
    "KF_AGENT_MAX_TOKENS를 올리거나 더 짧은 인자로 다시 호출할 것"
)


def _parse_tool_call_arguments(raw: str | None, truncated: bool = False) -> dict[str, Any]:
    """도구 호출 인자 JSON을 파싱한다 - 실패(또는 객체가 아닌 JSON)는 예외로 올려
    `provider.send()` 전체를, 나아가 `run_agent`를 죽이는 대신 `INVALID_JSON_ARGUMENTS_KEY`
    하나만 든 dict로 바꾼다. `agent/loop.py::_dispatch`가 핸들러를 부르기 전에 이 키를 보고
    평범한 도구 오류 결과(`error: invalid JSON arguments: ...`)로 돌려 모델이 다시 시도하게
    한다 - Qwen에서 관측된 깨진 JSON이 이 경로였다(`wiki/search-code-review-2026-09-14.md`
    결함11)."""
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        # 잘림이 원인이면 "깨진 JSON"으로 위장하지 않고 잘림이라고 말해준다.
        return {INVALID_JSON_ARGUMENTS_KEY: _TRUNCATED_ARGUMENTS_REASON if truncated else str(exc)}
    if not isinstance(parsed, dict):
        return {INVALID_JSON_ARGUMENTS_KEY: f"expected a JSON object, got {type(parsed).__name__}"}
    return parsed


def _to_openai_messages(messages: list[Message], system: str, echo_reasoning: bool = False) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        if m.role == "tool":
            out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content or ""})
            continue
        if m.role == "assistant":
            entry: dict = {"role": "assistant", "content": m.content}
            if echo_reasoning and m.reasoning and m.tool_calls:
                entry["reasoning"] = m.reasoning
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
        temperature: float | None = None,
        seed: int | None = None,
        timeout: float | None = None,
        reasoning: bool = False,
    ):
        # openai.AsyncOpenAI()'s zero-arg credential resolution only looks
        # for OPENAI_API_KEY, not OPENROUTER_API_KEY - same fallback-to-
        # placeholder-and-let-the-401-surface approach as
        # OpenRouterExtractor, for the same reason (keeps construction cheap
        # and lazy instead of raising immediately with no key). A local
        # server (vLLM) gets a dummy key instead of the OpenRouter one, so
        # that key never leaves for a self-hosted endpoint.
        if is_local_base_url(base_url):
            fallback_key = "EMPTY"
        else:
            fallback_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY") or "unset"
        self._client = client or openai.AsyncOpenAI(
            api_key=api_key or fallback_key,
            base_url=base_url,
            timeout=timeout if timeout is not None else settings.agent_request_timeout_s,
        )
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._seed = seed
        self._reasoning = reasoning
        self._extra_body = _reasoning_extra_body(base_url, reasoning)
        self._semaphore = asyncio.Semaphore(max_concurrency or settings.max_concurrent_llm_requests)

    def _request_kwargs(self, messages: list[Message], tools: list[ToolSpec], system: str) -> dict:
        kwargs: dict = dict(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=_to_openai_messages(messages, system, echo_reasoning=self._reasoning),
        )
        if tools:
            kwargs["tools"] = _to_openai_tools(tools)
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature
        if self._seed is not None:
            kwargs["seed"] = self._seed
        if self._extra_body:
            kwargs["extra_body"] = self._extra_body
        return kwargs

    @staticmethod
    def _wrap_api_error(exc: openai.APIError) -> AgentError:
        message = f"agent chat call failed (openai-compatible): {exc}"
        if isinstance(exc, openai.AuthenticationError):
            message = OPENAI_COMPATIBLE_AGENT_CREDENTIAL_HINT_KO
        return AgentError(message)

    @openai_retry()
    async def send(self, messages: list[Message], tools: list[ToolSpec], system: str) -> AssistantTurn:
        kwargs = self._request_kwargs(messages, tools, system)
        async with self._semaphore:
            try:
                response = await self._client.chat.completions.create(**kwargs)
            except RETRYABLE_OPENAI_ERRORS:
                raise
            except openai.APIError as exc:
                raise self._wrap_api_error(exc) from exc

        choice = response.choices[0]
        msg = choice.message
        return _build_turn(
            msg.content,
            _reasoning_of(msg),
            [(tc.id, tc.function.name, tc.function.arguments) for tc in (msg.tool_calls or [])],
            choice.finish_reason,
        )

    @openai_retry()
    async def send_stream(
        self, messages: list[Message], tools: list[ToolSpec], system: str, on_text: Callable[[str], None]
    ) -> AssistantTurn:
        kwargs = self._request_kwargs(messages, tools, system)
        content: list[str] = []
        reasoning: list[str] = []
        calls: dict[int, list[str]] = {}  # index -> [id, name, arguments]
        finish_reason = None
        started = time.perf_counter()
        ttft_s = None
        async with self._semaphore:
            try:
                stream = await self._client.chat.completions.create(stream=True, **kwargs)
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    finish_reason = choice.finish_reason or finish_reason
                    delta = choice.delta
                    if r := _reasoning_of(delta):
                        reasoning.append(r)
                    for tc in getattr(delta, "tool_calls", None) or []:
                        slot = calls.setdefault(tc.index, ["", "", ""])
                        slot[0] = tc.id or slot[0]
                        if tc.function is not None:
                            slot[1] += tc.function.name or ""
                            slot[2] += tc.function.arguments or ""
                    if delta.content:
                        content.append(delta.content)
                        if ttft_s is None:
                            ttft_s = time.perf_counter() - started
                            logger.info("agent stream TTFT %.3fs (model=%s)", ttft_s, self._model)
                        on_text(delta.content)
            except RETRYABLE_OPENAI_ERRORS:
                raise
            except openai.APIError as exc:
                raise self._wrap_api_error(exc) from exc

        turn = _build_turn(
            "".join(content) or None,
            "".join(reasoning) or None,
            [tuple(calls[i]) for i in sorted(calls)],
            finish_reason,
        )
        turn.ttft_s = ttft_s
        return turn


def _reasoning_of(obj: Any) -> str | None:
    """vLLM(구버전)·DeepSeek 계열은 `reasoning_content`, OpenRouter·최신 vLLM은 `reasoning`."""
    return getattr(obj, "reasoning_content", None) or getattr(obj, "reasoning", None) or None


def _build_turn(
    text: str | None, reasoning: str | None, raw_calls: list[tuple[str, str, str | None]], finish_reason: str | None
) -> AssistantTurn:
    truncated = finish_reason == "length"
    tool_calls = [
        ToolCall(id=id_, name=name, arguments=_parse_tool_call_arguments(args, truncated=truncated))
        for id_, name, args in raw_calls
    ]
    stop_reason: StopReason
    if tool_calls:
        stop_reason = "tool_use"
        if truncated:
            logger.warning("tool call cut off by max_tokens (finish_reason=length)")
    elif truncated:
        stop_reason = "max_tokens"
    else:
        stop_reason = "end_turn"
    return AssistantTurn(text=text, tool_calls=tool_calls, stop_reason=stop_reason, reasoning=reasoning)
