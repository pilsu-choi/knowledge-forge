"""로컬 `claude` CLI(claude_agent_sdk)를 한 번 호출하는 **텍스트 전용**
ChatProvider - `extraction/claude_agent_sdk_extractor.py`와 같은 이유로 존재한다:
목적은 능력이 아니라 **인증**이다. Claude Code가 이미 로그인돼 있는 머신이면
별도 ANTHROPIC_API_KEY 없이 오프라인 평가 커맨드(`eval`/`eval-ir`/`eval-gen`)를
돌릴 수 있어야 한다.

**에이전트 하네스에는 들어가지 않는다.** `config.py`의 `agent_provider`는 여전히
`Literal["anthropic", "openai_compatible"]`이고 여기에 "claude_agent_sdk"를
추가하지 않았다 - wiki/agent-runtime.md의 "claude_agent_sdk는 harness의 어떤
모드에도 들어오지 않는다"(그 SDK가 자기 tool-call 루프를 내부에 갖고 있어서
`agent/loop.py`와 이중 루프가 된다)는 판단은 그대로 유효하다. 이 클래스는
`get_chat_provider(provider="claude_agent_sdk")`로 **명시적으로 요청할 때만**
만들어지고, `tools`가 비어 있지 않으면 곧바로 거부한다 - 즉 "한 번 보내고 한 번
받는" 단발 구조화 출력 호출(judge/제약 추출/질의 생성)에만 쓰인다.
"""

from __future__ import annotations

import asyncio
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, CLIConnectionError, ResultMessage, query

from knowledge_forge.chat_provider import AssistantTurn, ChatProvider, Message, ToolSpec
from knowledge_forge.config import settings
from knowledge_forge.extraction.auth import CLAUDE_AGENT_SDK_CREDENTIAL_HINT_KO

DEFAULT_MODEL = "claude-haiku-4-5-20251001"


def _render(messages: list[Message]) -> str:
    """대화 이력을 한 프롬프트 문자열로 편다. 이 provider를 쓰는 경로는 전부
    user 메시지 하나짜리라 사실상 그 하나를 꺼내는 것이고, 여러 개가 들어오면
    역할을 접두사로 붙여 순서만 보존한다(tool 메시지는 애초에 올 수 없다 -
    `send`가 tools를 거부하므로)."""
    if len(messages) == 1 and messages[0].content:
        return messages[0].content
    return "\n\n".join(f"[{m.role}] {m.content or ''}" for m in messages)


class ClaudeAgentSDKChatProvider(ChatProvider):
    def __init__(self, model: str = DEFAULT_MODEL, cli_path: str | None = None, query_fn: Any = query):
        self._model = model
        self._cli_path = cli_path or settings.claude_agent_sdk_cli_path
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_llm_requests)
        # 테스트에서 가짜 async generator로 갈아끼우는 지점(추출기와 동일).
        self._query_fn = query_fn

    async def send(self, messages: list[Message], tools: list[ToolSpec], system: str) -> AssistantTurn:
        if tools:
            raise NotImplementedError(
                "claude_agent_sdk provider는 도구 호출을 지원하지 않습니다 - "
                "오프라인 평가(eval/eval-ir/eval-gen)의 단발 호출 전용입니다. "
                "에이전트에는 KF_AGENT_PROVIDER=anthropic|openai_compatible을 쓰세요."
            )
        options = ClaudeAgentOptions(
            model=self._model,
            cli_path=self._cli_path,
            system_prompt=system,
            # 추출기와 같은 이유로 내장 도구를 전부 끈다: 이 호출은 파일을 읽거나
            # 명령을 실행할 이유가 없다.
            tools=[],
            max_turns=1,
        )
        async with self._semaphore:
            try:
                result: ResultMessage | None = None
                async for message in self._query_fn(prompt=_render(messages), options=options):
                    if isinstance(message, ResultMessage):
                        result = message
            except CLIConnectionError as exc:
                # "claude 바이너리가 없음"과 "띄웠지만 붙지 못함"이 모두 여기로
                # 온다 - 어느 쪽이든 인증할 대상 자체가 없다는 뜻이다.
                raise RuntimeError(CLAUDE_AGENT_SDK_CREDENTIAL_HINT_KO) from exc
        text = result.result if result is not None else None
        # 실패는 예외가 아니라 `text=None`으로 돌려준다 - 이 provider를 쓰는
        # 호출부(judge/gen)는 파싱 실패를 이미 "채점/생성하지 않고 건너뜀"으로
        # 처리하고 있고, 배치 한 건의 실패가 전체를 죽이면 안 된다.
        if result is not None and (result.is_error or result.subtype != "success"):
            text = None
        return AssistantTurn(text=text, tool_calls=[], stop_reason="end_turn")
