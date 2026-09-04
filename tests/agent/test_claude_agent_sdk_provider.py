"""로컬 `claude` CLI 경유 텍스트 전용 ChatProvider - 실제 CLI를 띄우지 않고
`query_fn`을 가짜 async generator로 갈아끼워 계약만 확인한다.

이 provider의 존재 이유는 능력이 아니라 인증(ANTHROPIC_API_KEY 없이 오프라인
평가 커맨드를 돌리는 것)이라, 여기서 지켜야 할 계약도 두 가지뿐이다:
도구 호출은 절대 받지 않는다, 그리고 실패는 예외가 아니라 `text=None`으로
돌려준다(배치 한 건의 실패가 전체를 죽이면 안 된다).
"""

import pytest
from claude_agent_sdk import ResultMessage

from knowledge_forge.agent.providers.claude_agent_sdk import ClaudeAgentSDKChatProvider
from knowledge_forge.chat_provider import Message, ToolSpec


def _result(text, subtype="success", is_error=False) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="s",
        result=text,
    )


def _query_fn(result, captured=None):
    async def _fn(prompt, options):
        if captured is not None:
            captured["prompt"] = prompt
            captured["options"] = options
        yield result

    return _fn


async def test_send_returns_result_text_and_disables_builtin_tools():
    captured: dict = {}
    provider = ClaudeAgentSDKChatProvider(query_fn=_query_fn(_result('{"queries": []}'), captured))

    turn = await provider.send([Message(role="user", content="본문")], [], "시스템 프롬프트")

    assert turn.text == '{"queries": []}' and turn.tool_calls == []
    assert captured["prompt"] == "본문"
    assert captured["options"].system_prompt == "시스템 프롬프트"
    assert captured["options"].tools == []  # 이 호출은 파일을 읽거나 명령을 실행할 이유가 없다


async def test_send_refuses_tool_calls():
    """`agent_provider` Literal이 이 값을 받지 않는 것과 같은 이유의 이중 방어 -
    이 SDK는 자기 tool-call 루프를 갖고 있어 agent/loop.py와 이중 루프가 된다."""
    provider = ClaudeAgentSDKChatProvider(query_fn=_query_fn(_result("x")))
    spec = ToolSpec(name="t", description="d", input_schema={}, handler=None)

    with pytest.raises(NotImplementedError):
        await provider.send([Message(role="user", content="본문")], [spec], "sys")


@pytest.mark.parametrize(
    "result",
    [_result("x", subtype="error_max_turns"), _result("x", is_error=True), None],
)
async def test_send_reports_failure_as_none_text(result):
    """호출부(judge/gen)는 `text=None`/파싱 실패를 이미 '건너뜀'으로 처리한다 -
    예외를 던지면 배치 한 건이 전체를 죽인다."""

    async def _fn(prompt, options):
        if result is not None:
            yield result

    provider = ClaudeAgentSDKChatProvider(query_fn=_fn)

    turn = await provider.send([Message(role="user", content="본문")], [], "sys")

    assert turn.text is None


async def test_send_flattens_multi_turn_history():
    """평가 경로는 항상 user 메시지 하나지만, 여러 개가 오면 순서를 잃지 않아야 한다."""
    captured: dict = {}
    provider = ClaudeAgentSDKChatProvider(query_fn=_query_fn(_result("ok"), captured))

    await provider.send(
        [Message(role="user", content="첫째"), Message(role="assistant", content="둘째")], [], "sys"
    )

    assert captured["prompt"] == "[user] 첫째\n\n[assistant] 둘째"
