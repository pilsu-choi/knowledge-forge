# 개발 가이드

## 셋업

의존성 관리는 [`uv`](https://docs.astral.sh/uv/)를 사용한다.

```bash
uv sync
```

포맷/커넥터별 선택적 의존성(extra)이 필요하면 명시적으로 추가한다:

```bash
uv sync --extra xlsx --extra pptx --extra hwpx --extra slack --extra google --extra notion --extra dev
```

| extra | 패키지 | 용도 |
|---|---|---|
| `xlsx` | `openpyxl` | XLSX 파싱 |
| `pptx` | `python-pptx` | PPTX 파싱(표/차트/그림) |
| `hwpx` | `python-hwpx` | HWPX 파싱 |
| `slack` | `slack-sdk` | `SlackCollector` |
| `google` | `google-api-python-client`, `google-auth` | `GmailCollector`/`GoogleDriveCollector` 공유 |
| `notion` | `notion-client` | `NotionCollector` |
| `korean` | `kss` | 한국어 문장 분리 품질 향상(선택, 첫 호출 시 모델 로딩이 느림) |
| `dev` | `pytest`, `pytest-asyncio`, `pytest-mock`, `reportlab` | 테스트/픽스처 생성 |

## Anthropic 자격 증명

앱 자체에서 API 키를 별도로 관리하지 않는다 — Anthropic Python SDK가 아래 순서로 자동
탐색한다.

1. `ANTHROPIC_API_KEY` 환경변수
2. `ANTHROPIC_AUTH_TOKEN` 환경변수
3. `ant auth login`으로 저장된 OAuth 프로필

`ant auth status`로 현재 활성 자격 증명을 확인할 수 있다. `ClaudeExtractor`/`VisionExtractor`
모두 `anthropic.AsyncAnthropic()`을 인자 없이 생성하므로, 위 자격 증명 중 하나만 설정되어 있으면
별도 코드 수정 없이 동작한다 — Knowledge Extraction과 Vision 추출(PDF/Image/PPTX의
표·이미지 처리, [`vision-extraction.md`](vision-extraction.md))이 자격 증명을 공유한다.
자격 증명이 전혀 없으면 `extract`/`schedule` 실행 시 파이프라인을 시작하기 전에
`extraction/auth.py`의 사전 점검이 한글 안내 메시지와 함께 즉시 종료시킨다(SDK 내부
스택트레이스 대신).

## Knowledge Forge 자체 설정값

`KF_` 접두사를 가진 환경변수(`src/knowledge_forge/config.py`의 `Settings`)는 저장소 루트의
`.env` 파일(`SettingsConfigDict(env_file=".env")`)을 자동으로 읽는다 — 실제 OS 환경변수로
export할 필요 없이 `.env`에 값을 채워 넣으면 된다. 템플릿은 [`.env.example`](../.env.example).

| 환경변수 | 기본값 | 설명 |
|---|---|---|
| `KF_EXTRACTION_MODEL` | `claude-opus-5` | Knowledge Extraction에 사용할 모델. `--model` CLI 옵션으로도 override 가능 |
| `KF_MAX_TOKENS_PER_CHUNK` | `4000` | Adaptive Chunker의 청크당 최대 토큰 예산. `--max-tokens` CLI 옵션으로도 override 가능 |
| `KF_MAX_CONCURRENT_LLM_REQUESTS` | `5` | Extractor/VisionExtractor 인스턴스마다 동시에 진행 가능한 LLM 요청 수 상한(`asyncio.Semaphore`). 한 문서의 여러 청크·표·이미지는 이 한도 안에서 동시 처리된다 |
| `KF_DATABASE_URL` | (없음, 저장 비활성) | Source/Document/Chunk/KnowledgeObject를 저장할 PostgreSQL DSN. [`storage.md`](storage.md) 참고 |
| `KF_DATA_DIR` | `./data` | 원본 바이트를 저장하는 디스크 blob 스토어 루트 |
| `KF_LLM_PROVIDER` | `anthropic` | `anthropic`, `openrouter`, `claude_agent_sdk` 중 하나 — Knowledge Extraction/Vision 추출 백엔드 선택. [`llm-providers.md`](llm-providers.md) 참고 |
| `KF_OPENROUTER_EXTRACTION_MODEL` | `anthropic/claude-haiku-4.5` | `KF_LLM_PROVIDER=openrouter`일 때 Knowledge Extraction 모델 슬러그 |
| `KF_OPENROUTER_VISION_MODEL` | `anthropic/claude-haiku-4.5` | `KF_LLM_PROVIDER=openrouter`일 때 Vision 추출 모델 슬러그 |
| `KF_CLAUDE_AGENT_SDK_VISION_MODEL` | `claude-haiku-4-5-20251001` | `KF_LLM_PROVIDER=claude_agent_sdk`일 때 Vision 추출 모델. 이미지 입력은 SDK 스트리밍 입력으로 보내며 실호출 검증 전이다([`vision-extraction.md`](vision-extraction.md)) |
| `KF_OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | OpenRouter API 엔드포인트 |
| `OPENROUTER_API_KEY` | (없음) | OpenRouter API 키(`KF_` 접두사 아님, `ANTHROPIC_API_KEY`처럼 `Settings`에 중복 저장하지 않음) |

SaaS 커넥터(Slack/Gmail/Google Drive/Notion) 자격 증명용 `KF_*` 변수는 별도 표로
["SaaS 커넥터 사용하기"](#saas-커넥터-사용하기) 절에 정리되어 있다.

## CLI 실행

### 1회성 추출

```bash
uv run knowledge-forge extract <path> [--source local|slack|notion|gdrive|gmail] \
  [--max-tokens 4000] [--model claude-opus-5] \
  [--output out.jsonl] [--pretty] [--store/--no-store] [--okf-dir ./okf] [--project-id PRJ-...]
```

- `<path>`: 단일 파일 또는 디렉토리(디렉토리면 재귀적으로 순회). `--source local`(기본값)일
  때만 필요하고, SaaS 소스에서는 무시된다.
- `--source`(기본값 `local`): `slack`/`notion`/`gdrive`/`gmail`을 주면 `path` 대신
  `collectors/saas/factory.py`의 해당 `build_*_collector()`가 `.env`(`KF_*`)로부터 조립한
  커넥터를 사용한다 — 파서/청커/추출/저장 배선은 로컬 파일과 완전히 동일하게 재사용된다.
- `--output`: 생략 시 stdout에 JSONL(한 줄에 Knowledge Object 하나) 출력.
- `--pretty`: 들여쓴 JSON 배열로 출력(사람이 읽기용, 대량 처리에는 비권장).
- `--store`(기본값): `KF_DATABASE_URL`이 설정되어 있으면 Postgres+디스크에 저장([`storage.md`](storage.md)).
  없으면 경고만 출력하고 출력 전용으로 계속 진행한다.
- `--okf-dir`: 지정하면 `--store`와 별개로, manifest.yaml + resources/objects/relations로
  된 파일 기반 Knowledge Base도 그 디렉터리에 기록한다([`okf.md`](okf.md)). `KF_SLACK_CHANNEL_PROJECT_MAP`으로
  한 실행이 여러 프로젝트에 걸쳐 있어도, `OKFWriter`가 아이템별 `project_id`를 보고 각자의
  `{root}/{project_id}/` 서브트리에 정확히 나눠 쓴다(Postgres `--store` 경로와 동일한 라우팅).

예시:

```bash
uv run knowledge-forge extract tests/fixtures/sample.md --pretty
uv run knowledge-forge extract --source slack --project-id PRJ-DEFAULT --no-store
```

### 이미 수집한 원본을 다시 파싱하기 (재적재)

```bash
uv run knowledge-forge reparse --source-type google_drive --dry-run
uv run knowledge-forge reparse --source-type google_drive \
    --extensions ".md,.html,.xlsx,.csv" --no-extract --no-embed
```

파서가 없거나 고장 나 있던 시점에 수집돼서 `sources` 행과 디스크 원본만 남고 `documents`가
안 생긴 소스를, **원본 API를 다시 부르지 않고** `sources.raw_path`의 바이트로 다시 파싱한다.
`--only-missing`(기본)이라 중간에 끊겨도 그냥 다시 돌리면 이어진다. `--extract`/`--embed`는
`extract`와 달리 기본이 꺼짐이다. 배경과 설계는 [`reingest-replay.md`](reingest-replay.md).

### 주기 실행 (스케줄링)

```bash
uv run knowledge-forge schedule <path> [--source local|slack|notion|gdrive|gmail] --interval-seconds 600
```

`extract`와 같은 파이프라인을 하나의 프로세스 안에서 반복 실행한다(Ctrl-C로 종료). `--source`
의미는 `extract`와 동일하다 — 다만 `local`은 여전히 `state_path` 기반으로 커서를 디스크에
영속화해 프로세스 재시작에도 이어서 동기화하지만, SaaS 소스는 아직 그런 영속화가 없어서
(`run_scheduled_sync`가 만든 Collector 인스턴스가 이 프로세스 생애주기 동안만 메모리에 커서를
들고 있음) `schedule` 프로세스를 재시작하면 SaaS 쪽은 처음부터(`initial_sync()`) 다시 돈다.
`--interval-seconds`의 기본값은 600초(10분)이고, 인터벌을 기다리지 않고 즉시 한 번 더
동기화하도록 `kill -USR1 <pid>` 또는 `curl -X POST http://127.0.0.1:8765/trigger`로 수동
트리거할 수 있다(HTTP 트리거는 기본 활성화, `--no-trigger-api`로 끌 수 있음). 자세한 내용은
[`scheduling.md`](scheduling.md).

### DB 스키마 초기화

```bash
uv run knowledge-forge db init
```

## 컨테이너로 실행하기

`Dockerfile`(uv 기반 멀티스테이지, `xlsx`/`pptx` extra 포함 — `hwpx`/`slack`/`google`/`notion`은
아직 이미지에 포함되어 있지 않음, 필요하면 `Dockerfile`의 `uv sync --extra ...` 목록에 추가) +
`docker-compose.yml`(앱 + Postgres)이 저장소 루트에 있다.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
docker compose up -d postgres              # Postgres만 먼저 (호스트 15432 포트로 노출)
docker compose run --rm app db init
docker compose run --rm app extract /app/input/sample.md --pretty
docker compose run --rm app schedule /app/input --interval-seconds 600
```

- 수집할 파일은 `./input`에 두면 컨테이너 안 `/app/input`에 읽기 전용으로 마운트된다.
- `./data`가 컨테이너의 `/app/data`(원본 blob 스토어)에 마운트되어 호스트에 영속화된다.
- `app` 서비스의 `KF_DATABASE_URL`은 compose 네트워크 안에서 `postgres` 서비스명으로 이미
  설정되어 있다(`postgresql://kf:kf@postgres:5432/knowledge_forge`) — 별도 설정 불필요.
- `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`은 호스트 쉘에서 그대로 컨테이너로 전달된다(이미지에
  하드코딩하지 않음).

## 테스트

```bash
uv run pytest
```

- 모델/청커/파서/컬렉터 테스트는 네트워크나 API 키 없이 동작한다.
- Extraction 테스트는 `anthropic.AsyncAnthropic` 모양의(메서드가 코루틴인) 가짜 클라이언트를
  모킹하므로 `ANTHROPIC_API_KEY` 없이도 통과한다. 파이프라인/파서/스케줄러 전체가 async라
  대부분의 테스트 함수도 `async def`다 — `pyproject.toml`의 `asyncio_mode = "auto"` 덕분에
  `@pytest.mark.asyncio` 데코레이터 없이 바로 동작한다.
- `tests/test_pipeline_e2e.py`는 LLM을 모킹한 상태로 전체 파이프라인(Collector→Parser→
  Chunker→Extraction)을 실행해 provenance 체인까지 검증하는 스모크 테스트다.

## 새 Parser 추가하기

1. `src/knowledge_forge/parsers/`에 `Parser`를 상속한 클래스를 만든다.
2. `supported_extensions: ClassVar[set[str]]`에 담당할 확장자를 지정한다.
3. `async def parse(self, item: RawItem, source_ref: SourceRef) -> Document`를 구현해 공통
   `Document` 구조(Heading/TextBlock/TableElement/ListElement/ImageElement)로 변환한다 — VLM
   호출이 없는 파서도 인터페이스 통일을 위해 `async def`로 선언한다(내부에 `await`가 없어도 됨).
4. `src/knowledge_forge/parsers/registry.py`의 `default_registry()`에 인스턴스를 등록한다.
5. `tests/parsers/`에 fixture 파일과 함께 테스트를 추가한다.

표/이미지를 VLM으로 처리해야 하는 파서라면(PDF/Image/PPTX 참고) 생성자에서
`vision_extractor: VisionExtractor | None = None`을 받아 저장하고, 실제 사용 시점에 지연
생성하는 헬퍼(`_vision_extractor()`)를 두는 패턴을 따른다 — 이렇게 하면 테스트에서 가짜
`VisionExtractor`를 주입해 Anthropic 자격 증명 없이도 파서를 검증할 수 있고(예:
`tests/parsers/test_pdf_vlm_hybrid.py`), 표/이미지가 없는 문서는 애초에 자격 증명을
건드리지 않는다.

## SaaS 커넥터 사용하기

Slack/Gmail/Google Drive/Notion 커넥터(`src/knowledge_forge/collectors/saas/`)는 전부 실구현
되어 있고, `knowledge-forge extract`/`schedule`의 `--source {slack,notion,gdrive,gmail}`로
바로 실행할 수 있다(위 [CLI 실행](#cli-실행) 참고). 인증된 SDK 클라이언트를 직접 만들 필요는
없다 — `collectors/saas/factory.py`의 `build_*_collector()`가 아래 `KF_*` 설정(`.env`)을
읽어서 클라이언트 생성부터 커넥터 조립까지 대신하고, CLI가 그걸 호출한다. 필요한 extra
(`slack`/`google`/`notion`, 자세한 내용은 [셋업](#셋업))가 설치되어 있어야 한다.

CLI를 거치지 않고 커넥터만 직접 조립해 커스텀 스크립트에서 쓰고 싶다면:

```python
from knowledge_forge.collectors.saas.factory import build_slack_collector
from knowledge_forge.pipeline import run_pipeline
# ... parser registry / chunker / extractor 등은 cli.py의 extract()가 하는 것과 동일하게 준비

collector = build_slack_collector()  # KF_SLACK_BOT_TOKEN / KF_SLACK_CHANNELS(또는 CHANNEL_PROJECT_MAP)를 읽음
async for outcome in run_pipeline(collector, registry, chunker, extractor):
    ...
```

빠진 설정이 있으면 `build_*_collector()`가 어떤 `KF_*` 변수가 필요한지 담은
`RuntimeError`를 즉시 던진다.

### SaaS 커넥터 자격 증명 (`KF_*`)

| 환경변수 | 대상 커넥터 | 설명 |
|---|---|---|
| `KF_SLACK_BOT_TOKEN` | Slack | Bot Token (`xoxb-...`) |
| `KF_SLACK_CHANNELS` | Slack | 동기화할 채널 ID, 콤마 구분 (예: `C0123,C0456`). `KF_SLACK_CHANNEL_PROJECT_MAP`이 있으면 생략 가능(그 맵의 키가 채널 목록이 된다) |
| `KF_SLACK_CHANNEL_PROJECT_MAP` | Slack | 선택: 채널 ID → project_id JSON(인라인 또는 파일 경로), 예: `{"C0123":"PRJ-A","C0456":"PRJ-B"}`. 봇 하나가 여러 프로젝트의 채널을 동시에 수집할 때, 채널별로 올바른 `project_id`를 태깅한다(맵에 없는 채널은 `--project-id`로 넘긴 값으로 폴백). `run_pipeline`은 `RawItem.project_id`가 있으면 그걸, 없으면 실행 전체에 붙는 `--project-id`를 쓴다 |
| `KF_NOTION_TOKEN` | Notion | Integration 토큰 |
| `KF_NOTION_DATABASE_ID` | Notion | 특정 데이터베이스로 범위를 좁힐 때만 설정 (없으면 `search()`로 접근 가능한 모든 페이지) |
| `KF_GDRIVE_CREDENTIALS_JSON` | Google Drive | 서비스 계정 JSON — 인라인 값 또는 파일 경로 둘 다 허용(`json.loads` 실패 시 경로로 재시도) |
| `KF_GDRIVE_FOLDER_ID` | Google Drive | 동기화 대상 폴더 ID (없으면 서비스 계정이 볼 수 있는 전체 파일) |
| `KF_GMAIL_CREDENTIALS_PATH` | Gmail | OAuth 클라이언트 설정 파일 경로 — 토큰을 최초 발급할 때만 쓰이고, factory 자체는 아직 이 파일로 인터랙티브 OAuth 동의 플로우를 수행하지 않는다 |
| `KF_GMAIL_TOKEN_PATH` | Gmail | 이미 발급된 user OAuth 토큰(`token.json`) 경로 — **필수**, 이 파일이 없으면 factory가 바로 에러를 낸다. 만료됐고 `refresh_token`이 있으면 factory가 자동으로 갱신해 같은 경로에 다시 쓴다 |
| `KF_GMAIL_QUERY` | Gmail | Gmail 검색 쿼리로 동기화 범위 제한 (예: `label:PI/CANDIDATE`) |

Gmail은 서비스 계정 위임이 아니라 사전에 발급된 user OAuth 토큰을 전제로 한다 — 최초 토큰
발급(브라우저 동의 화면을 띄우는 `InstalledAppFlow` 등)은 이번 범위 밖이라 별도로 한 번
수행해서 `KF_GMAIL_TOKEN_PATH`에 결과 `token.json`을 둬야 한다.

각 커넥터가 정확히 어떤 API를 호출하는지는 클래스 docstring과
[`saas-connectors.md`](saas-connectors.md)를 참고. 전부 모킹 테스트로만 검증되어 실제
계정으로는 아직 라이브 검증되지 않았다 — 자세한 내용과 후속 계획(CLI 연동, Gmail 최초 OAuth
플로우)은 [`roadmap.md`](roadmap.md) 참고.

## 새 SaaS Collector 추가하기

1. `src/knowledge_forge/collectors/saas/`에 `SaaSCollector`를 상속한 클래스를 만든다.
2. 생성자에서 실제 SDK 클라이언트(예: `slack_sdk.WebClient`)를 주입받도록 설계한다.
3. `initial_sync()`/`incremental_sync(cursor)`/`get_cursor()`를 구현하고, 변경/삭제 감지는
   `RawItem.content_hash`/`RawItem.deleted`로 표현한다.
4. `SaaSCollector._paginate()` 헬퍼를 사용해 페이지네이션 API를 순회할 수 있다.
5. 다른 파이프라인 단계(Parser 이후)는 전혀 수정할 필요가 없다.
