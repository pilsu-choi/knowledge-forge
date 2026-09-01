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
| `dev` | `pytest`, `pytest-mock`, `reportlab` | 테스트/픽스처 생성 |

## Anthropic 자격 증명

앱 자체에서 API 키를 별도로 관리하지 않는다 — Anthropic Python SDK가 아래 순서로 자동
탐색한다.

1. `ANTHROPIC_API_KEY` 환경변수
2. `ANTHROPIC_AUTH_TOKEN` 환경변수
3. `ant auth login`으로 저장된 OAuth 프로필

`ant auth status`로 현재 활성 자격 증명을 확인할 수 있다. `ClaudeExtractor`/`VisionExtractor`
모두 `anthropic.Anthropic()`을 인자 없이 생성하므로, 위 자격 증명 중 하나만 설정되어 있으면
별도 코드 수정 없이 동작한다 — Knowledge Extraction과 Vision 추출(PDF/Image/PPTX의
표·이미지 처리, [`vision-extraction.md`](vision-extraction.md))이 자격 증명을 공유한다.
자격 증명이 전혀 없으면 `extract`/`schedule` 실행 시 파이프라인을 시작하기 전에
`extraction/auth.py`의 사전 점검이 한글 안내 메시지와 함께 즉시 종료시킨다(SDK 내부
스택트레이스 대신).

## Knowledge Forge 자체 설정값

`KF_` 접두사를 가진 환경변수(`src/knowledge_forge/config.py`의 `Settings`):

| 환경변수 | 기본값 | 설명 |
|---|---|---|
| `KF_EXTRACTION_MODEL` | `claude-opus-5` | Knowledge Extraction에 사용할 모델. `--model` CLI 옵션으로도 override 가능 |
| `KF_MAX_TOKENS_PER_CHUNK` | `4000` | Adaptive Chunker의 청크당 최대 토큰 예산. `--max-tokens` CLI 옵션으로도 override 가능 |
| `KF_DATABASE_URL` | (없음, 저장 비활성) | Source/Document/Chunk/KnowledgeObject를 저장할 PostgreSQL DSN. [`storage.md`](storage.md) 참고 |
| `KF_DATA_DIR` | `./data` | 원본 바이트를 저장하는 디스크 blob 스토어 루트 |
| `KF_LLM_PROVIDER` | `anthropic` | `anthropic` 또는 `openrouter` — Knowledge Extraction/Vision 추출 백엔드 선택. [`llm-providers.md`](llm-providers.md) 참고 |
| `KF_OPENROUTER_EXTRACTION_MODEL` | `anthropic/claude-haiku-4.5` | `KF_LLM_PROVIDER=openrouter`일 때 Knowledge Extraction 모델 슬러그 |
| `KF_OPENROUTER_VISION_MODEL` | `anthropic/claude-haiku-4.5` | `KF_LLM_PROVIDER=openrouter`일 때 Vision 추출 모델 슬러그 |
| `KF_OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | OpenRouter API 엔드포인트 |
| `OPENROUTER_API_KEY` | (없음) | OpenRouter API 키(`KF_` 접두사 아님, `ANTHROPIC_API_KEY`처럼 `Settings`에 중복 저장하지 않음) |

## CLI 실행

### 1회성 추출

```bash
uv run knowledge-forge extract <path> [--max-tokens 4000] [--model claude-opus-5] \
  [--output out.jsonl] [--pretty] [--store/--no-store]
```

- `<path>`: 단일 파일 또는 디렉토리(디렉토리면 재귀적으로 순회).
- `--output`: 생략 시 stdout에 JSONL(한 줄에 Knowledge Object 하나) 출력.
- `--pretty`: 들여쓴 JSON 배열로 출력(사람이 읽기용, 대량 처리에는 비권장).
- `--store`(기본값): `KF_DATABASE_URL`이 설정되어 있으면 Postgres+디스크에 저장([`storage.md`](storage.md)).
  없으면 경고만 출력하고 출력 전용으로 계속 진행한다.

예시:

```bash
uv run knowledge-forge extract tests/fixtures/sample.md --pretty
```

### 주기 실행 (스케줄링)

```bash
uv run knowledge-forge schedule <path> --interval-seconds 3600
```

`extract`와 같은 파이프라인을 하나의 프로세스 안에서 반복 실행한다(Ctrl-C로 종료). 자세한
내용은 [`scheduling.md`](scheduling.md).

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
docker compose run --rm app schedule /app/input --interval-seconds 3600
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
- Extraction 테스트는 `anthropic.Anthropic` 클라이언트를 모킹하므로 `ANTHROPIC_API_KEY` 없이도
  통과한다.
- `tests/test_pipeline_e2e.py`는 LLM을 모킹한 상태로 전체 파이프라인(Collector→Parser→
  Chunker→Extraction)을 실행해 provenance 체인까지 검증하는 스모크 테스트다.

## 새 Parser 추가하기

1. `src/knowledge_forge/parsers/`에 `Parser`를 상속한 클래스를 만든다.
2. `supported_extensions: ClassVar[set[str]]`에 담당할 확장자를 지정한다.
3. `parse(self, item: RawItem, source_ref: SourceRef) -> Document`를 구현해 공통 `Document`
   구조(Heading/TextBlock/TableElement/ListElement/ImageElement)로 변환한다.
4. `src/knowledge_forge/parsers/registry.py`의 `default_registry()`에 인스턴스를 등록한다.
5. `tests/parsers/`에 fixture 파일과 함께 테스트를 추가한다.

표/이미지를 VLM으로 처리해야 하는 파서라면(PDF/Image/PPTX 참고) 생성자에서
`vision_extractor: VisionExtractor | None = None`을 받아 저장하고, 실제 사용 시점에 지연
생성하는 헬퍼(`_vision_extractor()`)를 두는 패턴을 따른다 — 이렇게 하면 테스트에서 가짜
`VisionExtractor`를 주입해 Anthropic 자격 증명 없이도 파서를 검증할 수 있고(예:
`tests/parsers/test_pdf_vlm_hybrid.py`), 표/이미지가 없는 문서는 애초에 자격 증명을
건드리지 않는다.

## SaaS 커넥터 사용하기 (CLI 연동 전)

Slack/Gmail/Google Drive/Notion 커넥터(`src/knowledge_forge/collectors/saas/`)는 전부 실구현
되어 있지만, `knowledge-forge` CLI에는 아직 연결되어 있지 않다 — `extract`/`schedule`은
`LocalFileCollector`만 사용한다. 지금 SaaS 소스를 쓰려면 직접 스크립트를 작성해 인증된 SDK
클라이언트를 만들고 커넥터에 주입해야 한다:

```python
from slack_sdk import WebClient
from knowledge_forge.collectors.saas.slack import SlackCollector

client = WebClient(token="xoxb-...")
collector = SlackCollector(client=client, channels=["C0123456789"])
for item in collector.initial_sync():
    ...  # RawItem — 이후 pipeline.run_pipeline()에 다른 Collector처럼 넘기면 된다
```

각 커넥터가 정확히 어떤 API를 호출하는지는 클래스 docstring과
[`saas-connectors.md`](saas-connectors.md)를 참고. 전부 모킹 테스트로만 검증되어 실제
계정으로는 아직 라이브 검증되지 않았다 — 자세한 내용과 후속 계획(CLI 연동, 인증 관리)은
[`roadmap.md`](roadmap.md) 참고.

## 새 SaaS Collector 추가하기

1. `src/knowledge_forge/collectors/saas/`에 `SaaSCollector`를 상속한 클래스를 만든다.
2. 생성자에서 실제 SDK 클라이언트(예: `slack_sdk.WebClient`)를 주입받도록 설계한다.
3. `initial_sync()`/`incremental_sync(cursor)`/`get_cursor()`를 구현하고, 변경/삭제 감지는
   `RawItem.content_hash`/`RawItem.deleted`로 표현한다.
4. `SaaSCollector._paginate()` 헬퍼를 사용해 페이지네이션 API를 순회할 수 있다.
5. 다른 파이프라인 단계(Parser 이후)는 전혀 수정할 필요가 없다.
