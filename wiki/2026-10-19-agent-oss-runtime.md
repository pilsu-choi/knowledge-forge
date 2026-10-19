# 에이전트 오픈소스 모델 어댑터 보강 (2026-10-19)

[`improvement-plan-2026-09-15.md`](improvement-plan-2026-09-15.md)의 R1·R2·R4·R5·L5를 구현했다. 대상은 Qwen3.8-27B(OpenRouter `qwen/qwen3.8-27b`, 추후 온프렘 vLLM)이고, thinking은 끈 채로 진행한다.

## 결론

- thinking 끔이 기본값이다(`KF_AGENT_REASONING=false`). 서빙마다 파라미터가 달라 base_url로 고른다.
- 잘림·빈 답·thinking 전달이 조용히 사라지던 경로를 드러나게 했다.
- 최종 답변 스트리밍과 TTFT 측정 훅이 생겼다.
- **OpenRouter 실측은 못 했다.** 호출 4건이 모두 HTTP 402(크레딧 부족)로 거절됐다. 모델 카탈로그에는 `qwen/qwen3.8-27b`가 `reasoning`, `include_reasoning`, `reasoning_effort`, `seed`, `tools`를 지원한다고 나온다. 크레딧 충전 후 아래 확인 절차를 돌려야 한다.

## 변경 내용

| 항목 | 내용 |
|---|---|
| reasoning 토글 | OpenRouter는 `reasoning: {enabled: bool}`, 그 밖의 OpenAI 호환 서버(vLLM)는 `chat_template_kwargs: {enable_thinking: bool}`을 `extra_body`로 보낸다. `api.openai.com`은 모르는 파라미터를 400으로 거부하므로 아무것도 보내지 않는다 |
| R1 reasoning 전달 | `AssistantTurn.reasoning`, `Message.reasoning`. 응답의 `reasoning_content`/`reasoning`을 읽고, `KF_AGENT_REASONING=true`일 때만 도구 호출 assistant 메시지에 `reasoning`으로 되돌려 보낸다. 본문이 비고 reasoning만 있는 최종 턴은 `AgentError`(빈 문자열을 정답으로 돌려주지 않음) |
| R2 요청 설정 | `KF_AGENT_MAX_TOKENS`(4096), `KF_AGENT_TEMPERATURE`, `KF_AGENT_SEED`, `KF_AGENT_REQUEST_TIMEOUT_S`(120). 도구 호출 중 `finish_reason=length`로 잘리면 인자 오류 사유가 "max_tokens에서 잘렸다"로 나온다 |
| R4 | 로컬 base_url(localhost, 루프백, 사설망, 점 없는 호스트명, `.local`/`.internal`)이면 API 키 사전검사를 건너뛰고 더미 키를 쓴다(OpenRouter 키가 자체 호스팅 서버로 새지 않게). `agent` CLI의 `KF_OKF_DIR` 필수 조건을 없앴다 |
| L5 스트리밍 | `ChatProvider.send_stream(..., on_text)`. 기본 구현은 `send` 결과를 한 번에 흘리고, OpenAI 호환·Anthropic은 실제 스트림이다. `run_agent(on_text=)`, `run_query(on_text=)`, `AgentSession.ask(on_text=)`가 이를 쓴다 |

## 스트리밍과 TTFT

- `agent` CLI는 토큰을 stdout으로 흘리고 `[TTFT x.xxs, 총 y.yys]`를 stderr에 찍는다. 이 TTFT는 질문 전송부터 첫 토큰까지로, 도구 호출 턴을 포함한다.
- provider는 마지막 스트리밍 요청의 첫 토큰까지 시간을 `AssistantTurn.ttft_s`와 INFO 로그(`agent stream TTFT`)로 남긴다.
- 도구 호출 앞의 텍스트도 흘러나온다. 턴이 끝나야 최종 턴인지 알 수 있기 때문이다. Qwen은 보통 도구 호출 전에 본문을 내지 않는다.
- 스트림이 중간에 끊겨 재시도하면 이미 출력한 토큰이 다시 나올 수 있다.

## 확인 절차 (크레딧 충전 후)

```bash
set -a; . ./.env; set +a
# 끔: reasoning 필드가 없고 completion_tokens가 작아야 한다
curl -s https://openrouter.ai/api/v1/chat/completions -H "Authorization: Bearer $OPENROUTER_API_KEY" \
  -d '{"model":"qwen/qwen3.8-27b","max_tokens":300,"reasoning":{"enabled":false},"messages":[{"role":"user","content":"17*23은?"}]}'
# 켬: message.reasoning이 채워지고 completion_tokens_details.reasoning_tokens가 0보다 커야 한다
```

## 남은 문제

- 위 실측과, vLLM(`--reasoning-parser` 유무)에서 `enable_thinking=false`와 응답 필드 이름 확인.
- `KF_AGENT_REASONING=true`일 때 되돌려 보내는 키(`reasoning`)가 vLLM 버전에 따라 무시될 수 있다.
- 스트리밍은 CLI만 쓴다. API/UI 경로와 `delegate_task` 하위 호출은 비스트리밍이다.
