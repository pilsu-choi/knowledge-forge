# LLM 프로바이더 (Anthropic / OpenRouter / Claude Agent SDK)

Knowledge Extraction(`ClaudeExtractor`)과 Vision 추출(`VisionExtractor`, 표/이미지/차트 —
[`vision-extraction.md`](vision-extraction.md))이 호출하는 LLM 백엔드는 하드코딩이 아니라
설정으로 전환 가능한 **프로바이더 추상화**를 거친다. 기본값은 Anthropic(운영 검증된 경로)이고,
OpenRouter는 향후 다른 모델(비-Claude 포함)을 붙일 수 있도록 준비해 둔 대안 경로다.
Claude Agent SDK(`claude_agent_sdk`)는 Anthropic Messages API를 직접 호출하는 대신 `claude`
CLI를 서브프로세스로 구동하는 세 번째 경로로, **별도 `ANTHROPIC_API_KEY` 없이 이미 로그인된
Claude Code(OAuth)만으로 Knowledge Extraction을 실행**하고 싶을 때 쓴다 (Knowledge Extraction과
Vision 추출 둘 다 지원 - Vision 쪽은 `query()`의 스트리밍 입력 경로로 이미지 콘텐츠 블록을
실어 보낸다, 자세한 내용과 라이브 검증 상태는 [`vision-extraction.md`](vision-extraction.md)의
"claude_agent_sdk 아래 VLM이 조용히 전부 실패하던 문제" 참고).

## 왜 프로바이더를 나누는가

`Extractor` ABC(`extraction/base.py`)는 `extract(chunk, document, source) -> KnowledgeObject`
하나만 요구한다. `ClaudeExtractor`와 `OpenRouterExtractor`는 둘 다 이 ABC를 구현할 뿐이라,
파이프라인(`pipeline.py`)이나 CLI는 어느 쪽이 실제로 호출되는지 전혀 몰라도 된다. Vision 쪽도
동일하게 `VisionExtractor`/`OpenRouterVisionExtractor`가 같은 세 메서드
(`extract_table_markdown`/`describe_figure`/`transcribe_image`)를 제공한다(공식 ABC는 없고
파서들이 덕타이핑으로 사용).

## 전환 방법

```bash
export KF_LLM_PROVIDER=openrouter
export OPENROUTER_API_KEY=sk-or-...        # https://openrouter.ai 에서 발급
uv run knowledge-forge extract <path> --pretty
```

Claude Agent SDK로 전환하려면(별도 API 키 없이, 로컬에 로그인된 `claude` CLI를 그대로 사용):

```bash
export KF_LLM_PROVIDER=claude_agent_sdk
claude   # 아직 로그인 전이면 먼저 이걸로 브라우저 OAuth 로그인 (또는 CLAUDE_CODE_OAUTH_TOKEN 설정)
uv run knowledge-forge extract <path> --pretty
```

| 환경변수 | 기본값 | 설명 |
|---|---|---|
| `KF_LLM_PROVIDER` | `anthropic` | `anthropic`, `openrouter`, 또는 `claude_agent_sdk` |
| `KF_OPENROUTER_EXTRACTION_MODEL` | `anthropic/claude-haiku-4.5` | Knowledge Extraction에 쓸 OpenRouter 모델 슬러그 |
| `KF_OPENROUTER_VISION_MODEL` | `anthropic/claude-haiku-4.5` | Vision 추출(표/이미지/차트)에 쓸 OpenRouter 모델 슬러그 |
| `KF_OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | OpenRouter API 엔드포인트 |
| `OPENROUTER_API_KEY` | (없음) | OpenRouter API 키. `KF_` 접두사가 아님 — `ANTHROPIC_API_KEY`를 `Settings`에 중복 저장하지 않는 것과 같은 원칙으로, provider 클래스 생성 시점에 환경에서 직접 읽는다 |
| `KF_CLAUDE_AGENT_SDK_EXTRACTION_MODEL` | `claude-haiku-4-5-20251001` | Knowledge Extraction에 쓸 모델 (Claude Agent SDK 경로) |
| `KF_CLAUDE_AGENT_SDK_VISION_MODEL` | `claude-haiku-4-5-20251001` | Vision 추출(표/이미지/차트)에 쓸 모델 (Claude Agent SDK 경로) |
| `KF_CLAUDE_AGENT_SDK_CLI_PATH` | (없음 → PATH의 `claude`) | `claude` 바이너리가 PATH에 없을 때만 명시적 경로 지정 |
| `CLAUDE_CODE_OAUTH_TOKEN` | (없음) | `claude setup-token`으로 발급하는 장기 OAuth 토큰. `KF_` 접두사가 아님 — `claude` CLI가 직접 읽는 표준 환경변수라 여기서 중복 관리하지 않는다. headless/CI 환경에서 브라우저 로그인 대신 사용 |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` | (없음) | `claude` CLI 자체가 인식하는 표준 자격 증명 — 이미 로그인돼 있지 않아도 이 경로로 동작한다 |

`--model` CLI 옵션(`extract`/`schedule`)은 기본값이 `None`으로 바뀌어, 지정하지 않으면
활성 프로바이더 자신의 기본 모델을 쓴다(Anthropic이면 `claude-opus-5`, OpenRouter면
`KF_OPENROUTER_EXTRACTION_MODEL`) — 프로바이더와 무관하게 항상 Anthropic 모델명이 기본값으로
들어가던 이전 동작을 고쳤다.

## 모델 슬러그 형식

OpenRouter는 `provider/model-name` 형식을 쓴다 (`anthropic/claude-haiku-4.5`,
`openai/gpt-4o-mini`, `meta-llama/llama-3.1-70b-instruct` 등). 어떤 슬러그를 쓸지는 전적으로
설정값이며, 코드에는 특정 모델 계열에 대한 분기가 없다 — OpenRouter가 라우팅하는 모델이라면
원칙적으로 그대로 쓸 수 있다(단, 아래 한계 참고).

## 구현 위치

- `src/knowledge_forge/extraction/openrouter_extractor.py` — `OpenRouterExtractor`. `openai`
  패키지(OpenRouter가 OpenAI Chat Completions API를 그대로 미러링)로
  `client.chat.completions.create(..., response_format={"type":"json_schema", "json_schema":
  {...}})`를 호출해 구조화 출력을 받는다. `ExtractionResult.model_json_schema()`를 OpenAI
  strict 모드가 요구하는 형태(`additionalProperties: false` + 모든 프로퍼티를 `required`에
  포함)로 후처리하는 `_to_strict_json_schema()` 헬퍼가 포함되어 있다.
- `src/knowledge_forge/embedding/openrouter_provider.py` — `OpenRouterEmbeddingProvider`.
  `client.embeddings.create(model=..., input=[...])`만 호출한다(`dimensions`는 보내지 않는다 —
  OpenRouter 문서에 없는 파라미터라 모델별 동작이 불확실). 응답 순서 방어와 차원 검증
  (`embedding/validation.py`)이 추출 경로와 다른 점.
- `src/knowledge_forge/vision/openrouter_extractor.py` — `OpenRouterVisionExtractor`. 동일한
  `chat.completions.create`를 쓰되, 이미지는 Anthropic의 `{"type":"image","source":{...}}`가
  아니라 OpenAI 방식 `{"type":"image_url","image_url":{"url":"data:<mime>;base64,<...>"}}`
  블록으로 보낸다.
- `src/knowledge_forge/extraction/claude_agent_sdk_extractor.py` — `ClaudeAgentSDKExtractor`.
  `claude_agent_sdk.query(prompt=..., options=ClaudeAgentOptions(output_format={"type":
  "json_schema", "schema": ExtractionResult.model_json_schema()}, tools=[], max_turns=1, ...))`
  으로 `claude` CLI를 서브프로세스로 구동해 구조화 출력을 받는다. `tools=[]`로 모든 내장 도구를
  꺼서(파일 읽기/쓰기/명령 실행 없음) 순수 1회 completion 호출로 제한하고, 결과는
  `ResultMessage.structured_output`에서 꺼낸다.
- `src/knowledge_forge/vision/claude_agent_sdk_extractor.py` — `ClaudeAgentSDKVisionExtractor`.
  위 extractor와 달리 문자열 프롬프트가 아니라 `query(prompt=<AsyncIterable[dict]>, ...)`
  스트리밍 입력을 쓴다 - 문자열 프롬프트 경로는 `content`를 그 문자열로 고정해 이미지를
  실을 자리가 없고, 스트리밍 입력의 각 메시지 dict는 검증 없이 그대로 CLI stdin에 쓰이므로
  Anthropic 네이티브 이미지 콘텐츠 블록을 그 안에 넣을 수 있다. 구조화 출력 스키마는 없고
  (표/이미지 설명은 자유 텍스트) `ResultMessage.result`를 읽는다. 자세한 근거와 라이브 검증
  상태는 [`vision-extraction.md`](vision-extraction.md) 참고.
- `src/knowledge_forge/retry.py` — `anthropic_retry()`/`openai_retry()`/
  `claude_agent_sdk_retry()`가 각자의 재시도 조건에 대해 동일한 지수 백오프 정책을 공유한다.
  앞의 둘은 SDK 예외 타입(`RateLimitError`/`APIConnectionError`/`InternalServerError`)으로
  판단하지만, Claude Agent SDK 쪽의 `ResultError`/`CLIConnectionError`는 타입만으로는 재시도
  여부를 알 수 없어(HTTP 상태가 속성으로만 실림) `is_retryable_claude_agent_sdk_error()`라는
  술어 함수로 판단한다 - `CLINotFoundError`(바이너리 자체가 없음)는 재시도해도 같은 실패가
  반복되므로 제외.
- `src/knowledge_forge/extraction/factory.py`의 `get_extractor()`,
  `src/knowledge_forge/vision/factory.py`의 `get_vision_extractor()` — `KF_LLM_PROVIDER`를 보고
  셋 중 하나를 생성하는 유일한 분기점(Vision은 여전히 둘 중 하나). PDF/PPTX/DOCX/HWPX/Image
  파서의 지연 생성 헬퍼(`_vision_extractor()`)와 CLI의 `extract`/`schedule`,
  `scheduling/scheduler.py`가 모두 이 팩토리를 통해서만 프로바이더를 얻는다 — 하드코딩된
  `ClaudeExtractor(...)`/`VisionExtractor()` 호출은 남아있지 않다.
- `src/knowledge_forge/extraction/auth.py` — `has_resolvable_credentials()`/
  `credential_hint_ko()`가 `KF_LLM_PROVIDER`를 보고 어느 자격 증명(Anthropic vs
  `OPENROUTER_API_KEY` vs `claude` CLI/`CLAUDE_CODE_OAUTH_TOKEN`)을 점검/안내할지 분기한다.
  Claude Agent SDK 쪽은 `claude` 바이너리가 PATH(또는 `KF_CLAUDE_AGENT_SDK_CLI_PATH`)에
  존재하는지만 확인한다 - 실제 로그인 여부는 CLI를 직접 실행하지 않는 한 알 수 없어, 이 사전
  점검이 잡을 수 있는 건 "CLI 자체가 아예 없음" 케이스뿐이다(다른 두 프로바이더의 사전 점검과
  같은 한계). CLI의 사전 점검(`_ensure_authenticated`)이 이걸 쓴다.

## 임베딩도 OpenRouter로 붙일 수 있다 (별도 스위치)

`KF_LLM_PROVIDER`는 Knowledge Extraction/Vision의 백엔드만 고른다. 임베딩은
`KF_EMBEDDING_PROVIDER`라는 **별도 스위치**를 쓰며(`agent_provider`가 별도인 것과 같은 이유 —
추출은 Anthropic, 임베딩은 OpenRouter 같은 조합이 정당하다), `openrouter`로 두면
`embedding/openrouter_provider.py`가 OpenRouter의 OpenAI 호환 `/embeddings`를 호출한다. 자세한
내용은 [`embeddings.md`](embeddings.md)의 "OpenRouter / OpenAI 호환 엔드포인트" 절.

여기서 알아둘 두 가지:

- **임베딩 키만은 `KF_` 접두 설정(`KF_EMBEDDING_API_KEY`)으로도 넣을 수 있다.** 위 표의
  `OPENROUTER_API_KEY`/`ANTHROPIC_API_KEY`는 provider 클래스가 `os.environ`에서 직접 읽으므로
  셸 `export`가 필요한데, pydantic-settings는 `.env`를 `Settings`로만 읽고 `os.environ`에
  주입하지 않는다(이 저장소엔 `load_dotenv()`가 없다). 즉 **`.env`에 적은 `OPENROUTER_API_KEY`는
  어떤 경로로도 읽히지 않는다** — 추출 쪽도 마찬가지다. 임베딩만 `.env` 한 줄로 설정 가능하도록
  예외를 뒀고, 근거는 [`embeddings.md`](embeddings.md)의 "왜 `KF_EMBEDDING_API_KEY`라는 예외를
  뒀는가" 절에 있다.
- **임베딩 응답은 `index`로 재정렬하고 차원을 검증한다.** 게이트웨이 경유라 순서 보장이
  간접적이고, OpenRouter에는 3072/4096 차원 모델이 흔해 `schema.sql`의 `vector(1536)`와 어긋나기
  쉽기 때문이다. 추출 경로엔 없는 방어이므로 임베딩 provider를 복사해 새 백엔드를 만들 때
  같이 가져가야 한다.

## 코드 중복에 대한 설계 결정

`OpenRouterExtractor.extract()`는 `KnowledgeObject`를 만드는 로직을 `ClaudeExtractor.extract()`
에서 그대로 복사했다 — 공유 베이스 클래스로 묶지 않은 건 의도적이다. 두 프로바이더의 응답
형태(`response.parsed_output` vs 직접 파싱해야 하는 JSON 문자열)가 달라서, 억지로 공유하면
추상화가 프로바이더 세부사항을 흘리거나 이미 테스트된 `ClaudeExtractor`의 동작을 건드릴
위험이 있다. 약간의 중복이 그 위험보다 낫다는 판단.

## 검증 상태 (중요한 한계)

**OpenRouter 경로는 실제 OpenRouter API 키로 라이브 검증되지 않았다.** 이 환경엔
`OPENROUTER_API_KEY`가 없어서, `tests/extraction/test_openrouter_extractor.py`/
`tests/vision/test_openrouter_extractor.py`/`tests/test_provider_factory.py`/
`tests/embedding/test_openrouter_embedding_provider.py` 전부 OpenAI SDK
클라이언트를 모킹한 상태로만 검증했다(이 저장소의 다른 모든 외부 API 테스트와 동일한 철학 —
SaaS 커넥터([`saas-connectors.md`](saas-connectors.md)), HWPX 등과 같은 급의 한계).

구체적으로 라이브 검증되지 않은 부분:
- `response_format`의 strict json_schema 모드가 실제로 OpenRouter가 라우팅하는 각 모델에서
  요구사항대로 지켜지는지(모델마다 구조화 출력 지원 수준이 다름).
- `_to_strict_json_schema()`의 스키마 후처리가 OpenAI 쪽 실제 검증 로직과 정확히 맞는지.
- 이미지 콘텐츠(`image_url` + data URL)를 모든 대상 모델이 동일하게 처리하는지.

`OPENROUTER_API_KEY`가 있는 사용자가 직접 검증 후 이 절을 갱신하는 것을 전제로 한다.

**Claude Agent SDK 경로는 로그인된 `claude` CLI로 실제 라이브 검증되었다** (mock이 아니라 진짜
서브프로세스를 띄워 한 청크를 실제로 추출 - `KF_LLM_PROVIDER=claude_agent_sdk`만 설정하고
`ANTHROPIC_API_KEY`/`OPENROUTER_API_KEY` 없이 성공). 단위 테스트
(`tests/extraction/test_claude_agent_sdk_extractor.py`)는 다른 두 프로바이더와 동일하게
`query_fn`을 가짜 비동기 제너레이터로 주입해 모킹한다.

## 비용/지연

Anthropic 직접 호출과 마찬가지로 청크당(그리고 표/이미지 하나당) 1회 호출이 발생한다 — 어느
프로바이더를 쓰든 이 구조는 동일하다([`vision-extraction.md`](vision-extraction.md)의
"비용과 지연" 절 참고). OpenRouter로 전환하는 주된 동기는 비용 절감이 아니라 **모델 선택의
유연성**(다양한 벤더의 모델을 하나의 API로 시도해볼 수 있음)이다.

두 프로바이더 모두 `AsyncAnthropic`/`AsyncOpenAI`로 구현되어 있고, 각 Extractor/
VisionExtractor 인스턴스가 자체 `asyncio.Semaphore`로 동시 요청 수를 제한한다(기본 5,
`KF_MAX_CONCURRENT_LLM_REQUESTS`) — 한 문서의 여러 청크·표·이미지에 대한 호출이 순차가
아니라 이 한도 안에서 동시에 나가므로, 어느 프로바이더를 쓰든 지연(wall-clock latency)은
호출 횟수의 합이 아니라 `ceil(호출 횟수 / 동시성 상한)` 수준으로 줄어든다.
