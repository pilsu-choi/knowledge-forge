# Agent Runtime — 모델 벤더 중립 harness (Phase 1 구현됨)

`refs/hermes-agent`를 참고해 세운 knowledge-forge 자체 agent runtime(harness). `wiki/agent-search.md`
의 `file_search`/`vector_search`/`graph_search` 설계 중 **file_search를 실제로 들고 도는 에이전트
프로세스를 이번에 구현했다** — `vector_search`/`graph_search`는 `wiki/embeddings.md`가 다루는
임베딩 파이프라인이 API 경로(`POST /search/vector`, `GET /relations/expand`, agent-search.md 참고)
까지 이어진 뒤 Phase 2로 추가한다.

**핵심 요구사항: 모델 벤더에 종속되지 않는 범용 harness.** 대화형(`--interactive`)이든 1회성
쿼리든 예외 없이 자체 `ChatProvider` 추상화 위에서 돈다 — 특정 벤더 SDK가 도구 호출 루프 자체를
대신 돌려주는 방식(`claude_agent_sdk.ClaudeSDKClient`)에는 어떤 모드도 얹지 않는다(아래 참고).

## 왜 자체 `ChatProvider` 추상화 위에 짓는가

- `claude_agent_sdk`는 이미 base dependency이고 Knowledge Extraction에서 쓰인다
  (`llm-providers.md`) — 하지만 `ClaudeSDKClient`/`query()`는 `claude` CLI 서브프로세스를 띄워
  도구 호출 루프·시스템 프롬프트·컨텍스트 관리까지 SDK 내부에서 전부 대신 돌려주는 "완성된
  에이전트"라, harness가 조립해 쓸 "메시지 보내고 응답 받는" 원시 프리미티브가 아니고 Anthropic
  전용이다. 범용 harness의 코어 루프를 우리가 직접 소유해야 벤더를 바꿔 끼울 수 있으므로 harness
  코어에서 제외했다 — 어떤 모드(1회성/대화형)에도 예외 없이.
- `refs/hermes-agent`의 `anthropic_adapter.py`/`chat_completion_helpers.py`/`tool_executor.py`/
  `tool_dispatch_helpers.py`에서 구조만 참고했다: 내부 통일 메시지 포맷 ↔ 벤더별 변환, 도구
  결과를 신뢰 경계로 감싸는 패턴(prompt injection 방어), 도구 호출 반복 상한. 스트리밍/failover/
  동시 도구 실행 같은 hermes의 프로덕션 고도화 계층은 이번 스코프에 없다.
- 프로바이더는 **Anthropic 네이티브 + OpenAI-호환(OpenRouter 기본) 2종만** 직접 구현 —
  `llm-providers.md`가 Knowledge Extraction에서 이미 쓰는 조합과 동일한 선택. LiteLLM 등 외부
  통합 라이브러리는 쓰지 않는다.

## 구현된 모듈

```
src/knowledge_forge/search/
    service.py            # file_search() - OKF 파일 트리 키워드/frontmatter 검색, Postgres 불필요
src/knowledge_forge/agent/
    auth.py                # 프로바이더별 자격 증명 preflight 체크 (extraction/auth.py와 같은 역할, 별도 키)
    tools.py                # search.service를 ToolSpec으로 감싸는 얇은 어댑터 + write_plan 조립
    plan.py                 # PlanState/write_plan 도구 - 에이전트가 스스로 세우는 다단계 작업 계획
    loop.py                 # run_agent() - 벤더 중립 도구 호출 루프 (모든 모드가 공유)
    prompts.py               # 시스템 프롬프트 템플릿
    runtime.py                # run_query()(1회성) / AgentSession(--interactive REPL) - 둘 다 loop.py 그대로 사용
    providers/
        base.py             # Message/ToolCall/AssistantTurn/ToolSpec, ChatProvider(ABC)
        anthropic.py         # AnthropicChatProvider (Messages API)
        openai_compatible.py # OpenAICompatibleChatProvider (OpenRouter 기본, base_url로 전환 가능)
        factory.py            # get_chat_provider() - KF_AGENT_PROVIDER로 선택
```

CLI: `knowledge-forge agent "질문" [--project-id ID] [--provider anthropic|openai_compatible]
[--model ...] [--interactive]` (`cli.py`).

### `search/service.py::file_search`

`okf/reader.py`가 이미 제공하는 `list_projects`/경로 안전장치, `okf/writer._parse_markdown`을
재사용해 `resources/**/*.md` + `objects/{type}/**/*.md`를 스캔한다. 매칭은 title(가중치 3) >
description/tags(가중치 2) > body(가중치 1) 순으로 랭킹하고, `type` 필터가 주어지면 `resources/`는
아예 스캔에서 제외한다(그 "type"은 KO 타입이 아니라 source 타입이라 절대 매칭될 수 없으므로).

### `agent/loop.py::run_agent`

`(user_message, tools, system, provider, max_iterations, history=None) -> (answer, updated_messages)`.
`history`를 그대로 다음 호출에 넘기는 게 `--interactive` REPL이 별도 백엔드 없이 이 함수 하나로
멀티턴을 이어가는 방법(`agent/runtime.py::AgentSession`). 도구 결과는 `_wrap_untrusted()`로
"이건 데이터지 지시가 아니다" 델리미터에 감싸 돌려주고(hermes의 `_maybe_wrap_untrusted` 패턴),
`max_iterations`(기본 8, `KF_AGENT_MAX_ITERATIONS`)를 넘기면 `AgentError`.

### `agent/plan.py::PlanState` / `write_plan` 도구

에이전트가 여러 단계 작업을 스스로 계획하고 진행 상황을 추적할 수 있게 하는 도구. Claude Code의
TodoWrite와 같은 설계: 호출할 때마다 `PlanState.steps` 전체를 통째로 대체한다(부분 수정 아님) -
`loop.py`가 diff를 reconcile할 필요가 없다. `ToolSpec.handler`는 세션 컨텍스트를 모르므로
`make_plan_tool()`이 `PlanState` 인스턴스를 클로저로 잡는 식으로 상태를 붙인다.

`run_query`(1회성)는 그때그때 버려지는 `PlanState()`로 충분하지만, `AgentSession`은 세션당 하나의
`PlanState`를 만들어 `history`처럼 turn 사이에 유지한다(`session.plan`으로 노출) - `cli.py`의
`--interactive` 루프는 매 턴 뒤 `session.plan.steps`가 비어있지 않으면 체크리스트를 stderr에
출력한다. 시스템 프롬프트(`prompts.py`)는 단순 질문에는 쓰지 말 것 / 호출마다 전체 목록을 다시
보낼 것 / 동시에 in_progress는 하나뿐일 것을 명시한다(`plan.py::_validate_steps`가 후자를 강제).

### `agent/providers/`

`Message`/`ToolCall`은 이 프로젝트만의 내부 표현이고, 각 `ChatProvider.send()`가 매 호출마다
전체 히스토리를 그 벤더의 wire format으로 통째로 번역한다(상태 없는 완성 primitive) - Anthropic은
`tool_use`/`tool_result` 콘텐츠 블록(연속된 내부 `tool` 메시지를 하나의 `user` 메시지로 병합해야
하는 제약이 있음), OpenAI-호환은 `tool_calls`/`role=tool` 메시지. 두 어댑터 모두
`extraction/claude_extractor.py`/`extraction/openrouter_extractor.py`의 클라이언트 생성·재시도
(`anthropic_retry`/`openai_retry`, `retry.py`)·에러 래핑 패턴을 그대로 따른다.

## 설정 (config.py, `.env.example`)

`KF_AGENT_PROVIDER`(anthropic 기본) / `KF_AGENT_MODEL` / `KF_AGENT_OPENAI_COMPATIBLE_MODEL` /
`KF_AGENT_OPENAI_COMPATIBLE_BASE_URL` / `KF_AGENT_MAX_ITERATIONS`. `llm_provider`(Knowledge
Extraction용)와 별도 키로 뒀다 - 두 관심사가 다른 백엔드를 가리켜도 되게. 자격 증명은
`ANTHROPIC_API_KEY`/`OPENROUTER_API_KEY`/`OPENAI_API_KEY`를 그대로 재사용(새 env var 없음).

## 알려진 제약 (Phase 1 스코프)

- 스트리밍 미지원 - 한 턴이 끝나야 출력.
- 도구 호출이 여러 개 와도 순차 실행 - 읽기 전용 도구뿐이라 안전, 지연 시간만 손해.
- failover 없음 - 선택한 프로바이더가 실패하면 그대로 에러.
- `/search/*` 계열과 마찬가지로 접근 제어 없음 - 로컬/단일 사용자 실행 전제.

## 다음 단계

1. `vector_search`/`graph_search`를 `search/service.py`·`agent/tools.py`에 추가 (embeddings.md의
   파이프라인이 이미 있으므로 `POST /search/vector` 등 API 경로만 마저 구현하면 됨).
2. `knowledge-forge mcp-server`(stdio) - 외부 host(Claude Code/Claude Desktop 등) 노출.
3. 범용화(파일 생성/편집 도구, permission mode, Skills 연동) - 별도 설계 문서.
