# 로드맵

## 완료된 범위 (Phase 1 + Phase 2 + Phase 3 + Phase 4 + Phase 5 + Phase 6)

- **전체 아키텍처 스캐폴딩**: Collector/Parser/Adaptive Chunker/Knowledge Extraction/Knowledge
  Object 전 구간의 인터페이스와 모델을 PRD가 명시한 4개 SaaS + 8개 파일 포맷을 아우르도록 설계.
- **모든 파일 포맷 end-to-end 동작**: PDF/DOCX/Markdown/TXT/XLSX/PPTX/Image/**HWPX** → Parser →
  Adaptive Chunker → Claude 기반 Knowledge Extraction → Knowledge Object. PDF/Image/PPTX/HWPX/
  DOCX의 이미지, PDF의 표는 VLM 하이브리드로, PPTX 네이티브 차트는 결정론적 데이터 추출로
  처리한다([`vision-extraction.md`](vision-extraction.md), [`parsers.md`](parsers.md)).
- **HTML 파서 추가**: PRD가 명시한 8개 포맷에는 없었지만, `lxml.html` 기반으로 `.html`/`.htm`을
  규칙 기반으로 파싱하도록 추가했다. `<img>`는 다른 포맷과 달리 원본 바이트를 파일에서 바로 얻을
  수 없어(URL/경로 참조일 뿐) VLM을 적용하지 않고 `src`/`alt`만 기록한다 —
  [`parsers.md`](parsers.md)의 "HTML" 절 참고.
- **4개 SaaS 커넥터 전부 실구현**: Slack/Gmail/Google Drive/Notion — 인증된 클라이언트를
  주입받아 초기/증분 동기화 수행([`saas-connectors.md`](saas-connectors.md)). Slack은 실제
  워크스페이스 대상 연결성 스모크 테스트(채널 멤버십 확인 + `initial_sync()` 실행)까지 거쳤지만,
  나머지는 여전히 모킹 테스트만 검증됨, 4개 전부 실 계정으로의 전체 파이프라인(파서→청커→LLM
  추출→저장) 라이브 검증은 아직 없음 — 아래 1단계 참고.
- **SaaS 커넥터 `.env` 기반 인증 조립 + CLI 연동**: `collectors/saas/factory.py`의
  `build_*_collector()`가 `KF_SLACK_BOT_TOKEN`/`KF_NOTION_TOKEN`/`KF_GDRIVE_CREDENTIALS_JSON`/
  `KF_GMAIL_TOKEN_PATH` 등 `KF_*` 설정(저장소 루트 `.env`, `Settings(env_file=".env")`)을 읽어
  SDK 클라이언트 생성 + 커넥터 조립까지 대신하고, `extract`/`schedule`의
  `--source {slack,notion,gdrive,gmail}`이 이걸 호출한다(`cli.py`의 `_build_collector()`) —
  호출자가 더 이상 직접 SDK 클라이언트를 만들어 생성자에 주입하거나, factory가 만든 커넥터를
  직접 `run_pipeline()`에 넘길 필요가 없다([`dev-guide.md`](dev-guide.md#saas-커넥터-사용하기)).
  Slack은 추가로 `KF_SLACK_CHANNEL_PROJECT_MAP`(채널 ID → project_id)으로 한 봇이 여러
  프로젝트의 채널을 수집할 때도 `RawItem.project_id`를 채널별로 올바르게 태깅할 수 있다 —
  맵에 없는 채널은 `--project-id`로 폴백([`saas-connectors.md`](saas-connectors.md)).
  Gmail은 사전 발급된 user OAuth 토큰(`KF_GMAIL_TOKEN_PATH`)의 만료 갱신까지만 자동화했고,
  최초 토큰 발급(브라우저 동의 플로우)은 아직 없다 — 아래 1단계 참고.
- **Context Reconstruction**: Collector와 Parser 사이에 신설된 단계 — Slack은 스레드 그룹핑 +
  시간 간격 기반 Conversation Segmentation, Gmail은 `threadId` 기준 Thread Reconstruction으로
  원본 메시지 스트림을 semantic unit으로 병합한다. File/Notion/Google Drive는 항등(변경 없음).
  자세한 내용은 [`context-reconstruction.md`](context-reconstruction.md) 참고.
- **저장(Storage)**: 원본은 디스크(content-addressed), Source/Document/Chunk/KnowledgeObject는
  PostgreSQL — opt-in, `--store`/`KF_DATABASE_URL`([`storage.md`](storage.md)).
- **스케줄링**: `knowledge-forge schedule`로 프로세스 내 주기 실행
  ([`scheduling.md`](scheduling.md)).
- **API 서버 + Postgres 기반 Job Queue/Worker**: `knowledge-forge serve`(FastAPI)의 `POST
  /ingest`가 즉시 `202`+job_id를 응답하고, 별도 프로세스인 `knowledge-forge worker`가 `jobs`
  테이블을 `FOR UPDATE SKIP LOCKED`로 폴링해 실제 파이프라인을 실행한다 — 4단계 "Ingest와
  Process 분리"의 큐/워커 인프라 자체는 완료([`api.md`](api.md)). `local_file_ingest`에 더해
  `POST /ingest`의 `source` 필드로 SaaS ingest(`{source}_ingest`, 예: `slack_ingest`)도
  enqueue할 수 있다 — `worker.py`의 `_dispatch()`가 `collectors.saas.factory.SAAS_COLLECTOR_BUILDERS`를
  보고 CLI `--source`와 같은 이름으로 라우팅한다.
- **컨테이너화**: `Dockerfile` + `docker-compose.yml`(앱 + Postgres) — [`dev-guide.md`](dev-guide.md).
- **인증**: 활성 LLM Provider에 맞는 자격 증명(Anthropic: `ANTHROPIC_API_KEY`/`ant auth login`
  OAuth, OpenRouter: `OPENROUTER_API_KEY`) 사전 점검 + 친절한 에러 메시지.
- **LLM Provider 추상화**: Knowledge Extraction/Vision 추출 모두 `KF_LLM_PROVIDER`로
  Anthropic(기본, 운영 경로) ↔ OpenRouter(대체, 다양한 라우팅 모델 실험용)를 전환 가능 —
  [`llm-providers.md`](llm-providers.md). OpenRouter 경로는 모킹 테스트만 존재, 이 환경에
  `OPENROUTER_API_KEY`가 없어 라이브 검증은 못함.
- **비동기 전환(Phase 5)**: LLM을 호출하는 모든 경로(`ClaudeExtractor`/`OpenRouterExtractor`,
  `VisionExtractor`/`OpenRouterVisionExtractor`, `pipeline.run_pipeline`, VLM을 쓰는 5개 파서,
  `PostgresStore`/`PipelineStore`, `scheduling/scheduler.py`, `cli.py`)를 `asyncio` 기반으로
  전환했다. 한 문서 안의 청크 추출과 표/이미지 VLM 호출이 `asyncio.gather`로 동시 실행되고
  (요소 순서는 그대로 유지), 각 Extractor/VisionExtractor 인스턴스가 자체
  `asyncio.Semaphore`로 실제 동시 API 요청 수를 제한한다(기본 5,
  `KF_MAX_CONCURRENT_LLM_REQUESTS`). `schedule` 명령은 `BlockingScheduler`에서
  `AsyncIOScheduler`로 바뀌어 tick이 이벤트 루프 위 백그라운드 job으로 실행된다 —
  [`architecture.md`](architecture.md)의 Knowledge Extraction/Scheduling 절,
  [`scheduling.md`](scheduling.md) 참고. 의도적으로 그대로 둔 것: 로컬 파일 Collector/SaaS
  Collector/`RawBlobStore`/Chunker(병목이 아닌 CPU·로컬 디스크 작업)와, 여러 파일(문서)을
  동시에 처리하는 것(파이프라인의 바깥 루프는 여전히 파일 단위 순차) — 후자는 4단계의
  "Ingest와 Process 분리" 항목과 겹치는 별도 과제.
- **라이브 검증(부분)**: 이 환경에 정식 `ANTHROPIC_API_KEY`는 없지만, 이미 로그인된 Claude Code
  세션(claude.ai OAuth)을 `claude-agent-sdk`로 재사용해 Markdown 추출 경로와 PDF VLM 하이브리드
  경로(표+이미지) 둘 다 실제 Claude Haiku 호출로 end-to-end 검증함(총 $0.04, 일회성 검증
  스크립트로 진행, 프로젝트 코드에는 포함 안 됨). 이 방식은 호출당 최소 $0.01~0.07의 하네스
  오버헤드가 있어 운영 백엔드로는 부적합 — 운영은 여전히 `ANTHROPIC_API_KEY`(또는
  `OPENROUTER_API_KEY`) 기준.
- **Knowledge Object 모델을 typed envelope + graph relation으로 재설계 (Phase 5)**: 외부
  아키텍처 리뷰에서 지적된 "Chunk와 KnowledgeObject는 1:1이 아니다"/"Hierarchy를 유일한 조직
  원리로 삼지 않는다"는 원칙을 반영했다.
  - `Extractor.extract()`가 `KnowledgeObject` 하나가 아니라 `ExtractionOutcome`
    (`objects: list[KnowledgeObject]`, `relations: list[Relation]`)을 반환하도록 변경 —
    청크 하나가 여러 타입드 객체(Entity/Fact/Event/Decision/ActionItem/Requirement/Policy/
    Issue/Concept)로 펼쳐진다.
  - `KnowledgeObject.chunk: ChunkRef` 단일 필드를 `evidence: list[Evidence]`로 바꿔
    Chunk↔KnowledgeObject 카디널리티를 다대다로 표현(실제로 여러 청크를 하나의 LLM 호출로
    묶는 로직은 아직 없음 — 아래 5단계 참고).
  - 관계를 `KnowledgeObject` 내장 텍스트 triple이 아니라 두 `knowledge_object_id`를 잇는
    별도의 `Relation` 레코드(`models/relation.py`)로 분리.
  - `version`/`pipeline_version`/`source_hash`/`supersedes` versioning 필드 추가.
  - `OKFWriter`(`src/knowledge_forge/okf/`)로 manifest.yaml + resources/objects/relations
    파일 트리 — Postgres 없이도 존재하는 file-based Knowledge Base ([`okf.md`](okf.md)).
  - 자세한 설계 근거는 [`architecture.md`](architecture.md), 필드 스펙은
    [`knowledge-object-schema.md`](knowledge-object-schema.md) 참고.
- **Typed Knowledge Graph 고도화 (Phase 6, 옛 로드맵 5단계 4개 항목 모두 완료)**:
  - **다중 청크 근거**: `Extractor.extract()`가 청크 하나가 아니라 청크 묶음(`chunks:
    list[Chunk]`)을 받도록 바뀌었다 — `extraction/grouping.py::group_chunks()`가 한 문서의
    청크들을 `KF_EXTRACTION_CHUNK_GROUP_SIZE`개씩(기본 1, 기존과 동일한 청크당 호출 1회)
    순서대로 묶는다. 2 이상으로 올리면 인접한 여러 청크가 한 LLM 호출로 묶여
    `KnowledgeObject.evidence`가 실제로 여러 청크를 가리키게 된다.
  - **KnowledgeObject 간 유사도 기반 중복 제거/병합**: `dedup.py::merge_duplicates()`가 한
    문서 안에서(청크 그룹들에 걸쳐) 타입이 같고 텍스트가 유사한(기본
    `KF_DEDUP_SIMILARITY_THRESHOLD=0.9`, `difflib` 기반, `similarity.py`) 객체를 하나로
    합치고 `DUPLICATE_OF` `Relation`으로 감사 기록을 남긴다. **같은 문서 안 중복까지만
    자동으로 처리한다** — 서로 다른 문서/소스에 걸친 중복(예: Slack 대화 + 그걸 정리한 회의록
    문서)은 이 함수 자체는 재사용 가능하지만, 파이프라인에 자동으로 연결되어 있지는 않다(아래
    "후속 작업" 참고).
  - **추가 Organization View**: `OKFWriter.write_views()`가 `views/by_topic.yaml`/
    `by_document.yaml`(Project의 대용)/`timeline.yaml` 세 파생 인덱스를 `manifest.yaml`과
    같은 방식(트리 스캔 후 재생성)으로 만든다 — [`okf.md`](okf.md) 참고.
  - **`supersedes` 자동 채우기**: `extraction/versioning.py::resolve_supersedes()`가
    `store`가 설정된 경우(재추출 상황) 같은 `source_id`의 이전 실행 객체들과 타입+텍스트
    유사도(기본 `KF_SUPERSEDES_SIMILARITY_THRESHOLD=0.8`)를 비교해 새 객체의
    `supersedes`/`version`을 자동으로 채우고 `SUPERSEDES` `Relation`을 추가한다.
    `PostgresStore.list_knowledge_objects_by_source()`로 이전 객체를 조회한다(OKF 전용
    실행에는 아직 연결 안 됨 — 아래 참고).
  - 위 두 유사도 매칭 모두 `pipeline.run_pipeline`이 한 문서의 모든 청크 그룹을 모은 직후,
    저장/yield 전에 실행된다 — 그래서 `run_pipeline`은 이제 청크(그룹)당 하나가 아니라
    문서당 병합된 `ExtractionOutcome` 하나를 yield한다. 설계 근거는
    [`architecture.md`](architecture.md)의 "설계 결정: 중복 제거와 재추출 버전 관리는 같은
    유사도 원리를 공유한다" 참고.

## 제외 범위와 "왜 막지 않는가"

PRD 5장이 명시한 제외 범위는 Vector DB / Graph DB / Ontology-OWL / Search-RAG / Agent /
Dashboard / Chat UI다. 이들을 지금 만들지 않는 이유는 스코프 관리이지, 설계가 이들을 나중에
막기 때문이 아니다.

| 제외 항목 | 현재 설계가 막지 않는 이유 |
|---|---|
| Vector DB | `KnowledgeObject.summary`/`content`/`topics`는 이미 임베딩하기 좋은 평탄한 텍스트 필드로 분리되어 있다. 벡터 스토어에 넣을 때 `KnowledgeObject`를 그대로 소스 레코드로 사용하면 된다. |
| Graph DB | `KnowledgeObject`(노드)와 `Relation`(subject_id/object_id로 노드를 잇는 typed edge)이 이미 그래프 모델 그 자체다 — Phase 5에서 subject-predicate-object 텍스트 triple 대신 id 기반 edge로 바꿨다([`architecture.md`](architecture.md)). `relations` 테이블(PostgreSQL, [`storage.md`](storage.md)) 또는 OKF `relations/`([`okf.md`](okf.md))를 그대로 그래프 DB에 bulk-import하면 된다. |
| Ontology/OWL | `Entity.type`/`document_type`이 자유 문자열이라, 추후 온톨로지 클래스 체계로 매핑하는 정규화 레이어를 얹기만 하면 된다. 지금 스키마를 바꿀 필요가 없다. |
| Search/RAG | Knowledge Object 자체가 검색 인덱싱의 입력 단위로 설계되어 있다(청크 단위 + 요약 + 메타데이터). RAG 파이프라인은 이 위에 검색/재순위 레이어만 추가하면 된다. |
| Agent | Knowledge Extraction은 순수 함수형 파이프라인 단계(청크 → LLM 호출 → 구조화 출력)로, 에이전트형 도구 호출 루프가 필요 없다. 나중에 에이전트가 필요해지면 이 파이프라인을 도구(tool)로 감싸기만 하면 된다. |
| Dashboard / Chat UI | 순수 데이터 처리 파이프라인이라 UI 레이어와 결합되어 있지 않다. CLI/JSONL 출력이 어떤 UI 위에도 얹힐 수 있는 형태다. |

## 단계별 후속 계획

### 1단계 — SaaS 커넥터 CLI/인증 연동

커넥터 코드 자체는 완료됐고([`saas-connectors.md`](saas-connectors.md)), **`.env`(`KF_*`)
설정으로부터 인증된 SDK 클라이언트를 만들어 커넥터에 주입하는 부분(`collectors/saas/factory.py`)과
**`extract`/`schedule`의 `--source` 옵션으로 CLI에서 바로 실행하는 부분도 완료됐다**
(`cli.py`의 `_build_collector()`) — Slack Bot Token/Notion Integration 토큰/GDrive 서비스
계정/이미 발급된 Gmail user OAuth 토큰(만료 시 자동 갱신 포함)은 factory 하나로 커넥터까지
조립되고, `--source slack`처럼 지정하면 `extract`/`schedule`이 이미 갖춘 파서/청커/추출/저장
배선을 그대로 SaaS 커넥터에도 재사용한다. `schedule --source slack`처럼 스케줄러에 물릴 때는
`run_scheduled_sync(collector=...)`로 이미 만들어진 SaaS 컬렉터를 그대로 넘긴다 — 다만 이
경로는 `LocalFileCollector`의 `state_path`(디스크 영속 커서)에 대응하는 게 없어서, 커서가
프로세스 생애주기 동안만 메모리에 있다(`schedule` 재시작 시 SaaS 소스는 처음부터 다시 돈다).

Slack은 추가로 `channel_project_map`(`KF_SLACK_CHANNEL_PROJECT_MAP`)을 지원한다 — 봇 하나가
여러 프로젝트의 채널을 동시에 수집하는 구성(ProjectIntel의 `project_mapping` 테이블과 같은
문제의식)에서, 파이프라인 전체에 붙는 단일 `--project-id`로는 표현할 수 없는 채널별 프로젝트
귀속을 `RawItem.project_id`(신설, `pipeline._make_source`가 있으면 우선 사용)로 표현한다.

다음이 남아 있다:

- Gmail의 **최초** user OAuth 토큰 발급(브라우저 동의 화면을 띄우는 `InstalledAppFlow` 등) —
  factory는 이미 있는 `token.json`의 만료 갱신만 하고, 최초 발급 자체는 아직 자동화 안 됨.
  Slack App 설치(Bot Token 발급)와 Notion Integration 토큰 발급도 각 플랫폼 관리 화면에서
  수동으로 해야 한다(이건 플랫폼 쪽 1회성 설정이라 자동화 대상이 아님).
- rate limit 대응(현재 커넥터 코드에는 없음).
- 실제 계정/토큰으로 각 커넥터의 **전체 파이프라인** 라이브 검증(현재는 모킹 테스트 + Slack만
  연결성 스모크 테스트 — `factory` 자체는 fake SDK 모듈 주입 테스트만 거쳤다,
  `tests/collectors/test_saas_factory.py`). 특히 파서→청커→LLM 추출까지 이어지는 실제 실행은
  아직 어떤 SaaS 소스로도 검증되지 않았다 — 실제 계정 데이터를 LLM에 보내는 비용/데이터 민감성
  문제가 있어 신중한 확인 후 진행할 것.
- ~~Slack의 `KF_SLACK_CHANNEL_PROJECT_MAP`처럼 여러 프로젝트에 걸친 채널별 귀속이 `--okf-dir`
  (OKFWriter)에서도 올바르게 반영되도록~~ — 완료. `OKFWriter`가 더 이상 생성 시점의
  `--project-id` 하나로 루트를 고정하지 않고, `write_resource`/`write_objects`가 아이템별
  `Source.project_id`/`KnowledgeObject.source.project_id`를 보고 각자의
  `{root}/{project_id}/`에 쓴다(`Relation`은 자기 `Source`가 없어 `write_relations()`가
  `project_id`를 인자로 받는다). `write_manifest()`/`write_views()`는 실행이 건드린 프로젝트
  수만큼 `{project_id: path}`를 돌려준다 — [`okf.md`](okf.md) "다중 프로젝트 라우팅" 참고.
- ~~`knowledge-forge schedule`에도 `--okf-dir`~~ — 완료. `extract`처럼 실행 전체가 끝난 뒤
  한 번이 아니라, `build_tick`(`scheduling/scheduler.py`)이 **매 tick이 끝날 때마다**
  `write_manifest()`/`write_views()`를 다시 부른다 — 트리 스캔 기반 재생성이라 반복 호출해도
  안전하고, 증분 동기화로 새로 수집된 것들이 다음 tick을 기다리지 않고 그 tick 안에서 바로
  매니페스트/뷰에 반영된다([`okf.md`](okf.md), [`scheduling.md`](scheduling.md) 참고).
- ~~`serve`/`worker` job queue 경로에도 OKF 연결~~ — 완료. `POST /ingest`에 `okf_dir` 필드가
  추가돼 `project_id`/`max_tokens`/`model`과 같은 패턴으로 job payload에 실려가고,
  `worker.py`의 `_dispatch()`가 그 필드로 `OKFWriter(okf_dir, project_id=job.project_id)`를
  만들어 `run_local_ingest`/`run_saas_ingest`에 넘긴다. job은 1회성 실행이라(`schedule`의
  반복 tick과 달리) `ingest.py`의 `_run_collector`가 실행이 끝난 뒤 매니페스트/뷰를 한 번만
  재생성한다 — [`api.md`](api.md), [`okf.md`](okf.md) 참고. 이제 세 진입점(`extract`/
  `schedule`/`serve`+`worker`) 모두 OKF를 지원한다.

### 2단계 — 시각 자료 처리 고도화

- ~~HWPX~~ — `python-hwpx` 라이브러리 기반으로 완료. 단, 실제 한컴 생성 파일로는 검증 못함 —
  [`parsers.md`](parsers.md) 참고.
- ~~PPTX 네이티브 차트~~ — 래스터화 대신 결정론적 데이터 추출로 완료 —
  [`parsers.md`](parsers.md) 참고.
- ~~XLSX(`openpyxl`), PPTX(`python-pptx`) 실구현~~ — 완료.
- ~~Image OCR~~ — VLM으로 대체되어 시스템 의존성(tesseract-ocr) 자체가 사라짐, 완료.
- ~~DOCX 임베드 이미지~~ — VLM 처리로 완료.

### 3단계 — 운영 최적화

- **Batches API**: 파이프라인은 이제 청크당(그리고 표/이미지 하나당) 1회 LLM 호출을
  `asyncio`로 동시 실행한다(`KF_MAX_CONCURRENT_LLM_REQUESTS`로 동시성 제한, 완료 —
  [`architecture.md`](architecture.md) 참고). 이건 지연(latency)을 줄이는 것이지 호출
  횟수(=비용)는 그대로다. Anthropic Message Batches API(24시간 내 비동기 처리, 표준 대비 50%
  비용)로 전환하면 지연이 문제되지 않는 대량 배치 처리에서 비용 자체를 줄일 수 있다 — 실시간
  동시성과는 다른 축의 최적화라 별도 후속 과제로 남겨둔다. Knowledge Extraction과 Vision 추출
  양쪽 다 후보.
- **모델/프로바이더 비용 레버**: `--model`/`KF_EXTRACTION_MODEL`로 Anthropic 모델 등급을
  낮추거나(Opus→Sonnet→Haiku), `KF_LLM_PROVIDER=openrouter`로 전환해 다른 모델을 시도하는
  것도 비용 최적화 옵션 — [`llm-providers.md`](llm-providers.md) 참고. 단 OpenRouter는 라이브
  검증 전이라 실제 비용/품질 비교는 아직 못함.
- 토큰 카운팅 정확도 개선(`messages.count_tokens`로 청크 경계 최종 검증).
- 인덱싱/저장 레이어(위 "제외 범위" 표 참고) 연동은 이 파이프라인의 출력(JSONL 또는
  PostgreSQL의 `knowledge_objects` 테이블)을 그대로 소비하는 형태로 설계되어 있어 별도
  어댑터만 추가하면 된다.
- ~~저장(Storage)~~ — 완료([`storage.md`](storage.md)).
- ~~스케줄링~~ — 완료([`scheduling.md`](scheduling.md)).

### 4단계 — Source-aware Context Reconstruction

사용자가 제시한 설계 방향을 코드로 구현했다 —
[`context-reconstruction.md`](context-reconstruction.md) 참고. **1단계(SaaS CLI/인증 연동)가
끝나 `extract`/`schedule --source slack`으로 실행할 진입점은 이제 있다** — `run_pipeline`이
`SlackCollector`/`GmailCollector`를 받으면 자동으로 이 재구성을 거치도록 배선도 이미 끝나
있다. 다만 실제 계정 데이터로 이 재구성 로직 자체가 라이브 검증된 적은 아직 없다(위 1단계의
"전체 파이프라인 라이브 검증" 항목 참고). 아래 액션 아이템 중 1~2는 구현 완료, 3~5는 여전히
미구현이다.

**핵심 문제의식**: 현재 파이프라인은 "하나의 수집 단위(RawItem) = 하나의 독립적인 의미 단위"를
암묵적으로 가정한다(File Collector에는 맞는 가정 — 파일 자체가 독립적 정보 단위). 하지만
SaaS 소스, 특히 Slack은 이 가정이 깨진다: 메시지 하나만으로는 의미가 없고, 여러 메시지로
구성된 대화(conversation/episode) 전체가 Knowledge Extraction의 적절한 context 단위다. 예:

```
A: 배포 언제 할까요?          B: 오늘 오후 가능할 것 같습니다.
C: DB migration 아직...       B: 아 그러면 내일 오전으로 하죠.
A: 네 내일 10시로 합시다.
```

마지막 메시지("네 내일 10시로 합시다")만 청크로 추출하면 "무엇을 10시로 합시다"인지 사라진다.
전체 대화를 하나의 context로 묶어야 `{type: Decision, subject: Production Deployment,
decision: Deploy tomorrow at 10 AM, reason: DB migration not completed}` 같은 의미 있는
Knowledge가 나온다.

**핵심 설계 원칙 (액션 아이템)**:

1. ~~**Source ≠ Knowledge Unit**~~ — 완료. 각 Collector가 만든 `RawItem`을 곧바로 Chunk로
   넘기지 않고, `ContextReconstructor`(`src/knowledge_forge/context/`)가 Collector와 Parser
   사이에서 소스별로 재구성한다 — [`context-reconstruction.md`](context-reconstruction.md) 참고.
2. ~~**소스별 Context Reconstruction 전략**~~ — 완료:
   - File(PDF/DOCX/XLSX/PPTX/Image/HWPX): 파일 자체가 독립 정보 단위 — 지금처럼 Parsing →
     바로 Chunking (변경 없음, `NoopReconstructor`).
   - Notion: Page → Block/Section 구조가 이미 명확 — 지금처럼 즉시 Parsing (변경 없음).
   - GDrive(Google Docs 등): Document → 구조 Parsing — 지금처럼 즉시 Parsing (변경 없음).
   - **Gmail**: `GmailThreadReconstructor`가 `threadId`로 그룹핑 후 시간순 정렬해 하나의
     대화로 합친다.
   - **Slack**: `SlackConversationReconstructor`가 **Conversation Reconstruction →
     Conversation Segmentation**(스레드는 그 자체로 하나의 에피소드, 나머지는 시간 간격이
     `gap_seconds`를 넘으면 새 에피소드로 분할)을 수행한다.
3. **첨부파일은 항상 File Pipeline으로 라우팅** — 미구현. Slack/Gmail/Notion/GDrive 어디서 오든
   첨부파일이 발견되면 기존 File Collector→Parser 경로로 넘긴다(별도 재구현 없이 기존
   파이프라인 재사용). 현재 4개 SaaS 커넥터는 아직 첨부파일 감지/추출 자체를 하지 않음
   ([`saas-connectors.md`](saas-connectors.md)) — 이것도 1단계에서 함께 다뤄야 한다.
4. ~~**Ingest와 Process 분리**~~ — 큐/워커 인프라와 SaaS 연결 둘 다 완료:
   `knowledge-forge serve`(FastAPI)의 `POST /ingest`가 `jobs` 테이블(Postgres)에 enqueue하고
   즉시 `202`+job_id를 응답, 별도 프로세스 `knowledge-forge worker`가 `FOR UPDATE SKIP LOCKED`
   폴링으로 job을 집어 `run_pipeline`을 실행한다 — [`api.md`](api.md) 참고. `POST /ingest`의
   `source` 필드(CLI `--source`와 동일한 이름 체계)가 `local_file_ingest` 또는
   `{source}_ingest`(예: `slack_ingest`)를 enqueue하고, `worker.py`의 `_dispatch()`가
   `collectors.saas.factory.SAAS_COLLECTOR_BUILDERS`를 보고 라우팅한다 — 이 매핑에 새 SaaS
   커넥터가 추가되면 `_dispatch`가 자동으로 그 job_type도 처리하므로, 새 커넥터를 추가할 때마다
   `worker.py`를 따로 고칠 필요가 없다.
5. **스케줄링 성격 구분** — Slack/Gmail처럼 계속 생성되는 데이터는 polling(현재
   `knowledge-forge schedule`이 이 모델) 또는 향후 webhook/event-driven ingest가 맞고, 정적
   파일 소스는 최초 1회 + 변경 감지 재처리로 충분하다는 게 이미 맞게 설계되어 있음 — 다만
   Slack/Gmail의 짧은 polling 주기가 필요해질 경우 API rate limit 대응(3단계에서 이미 언급)이
   더 중요해진다.

### 5단계 — Typed Knowledge Graph 고도화 (Phase 6에서 완료, 위 "완료된 범위" 참고)

옛 5단계의 4개 항목(다중 청크 근거/유사도 기반 중복 제거·병합/추가 Organization View/
`supersedes` 자동 채우기) 모두 구현했다. 다만 완료 과정에서 스코프를 의도적으로 좁힌 지점이
있어, 그 나머지는 별도 후속 과제로 남는다:

- **중복 제거는 같은 문서 안까지만 자동** — `dedup.py::merge_duplicates()`는
  `pipeline.run_pipeline`이 한 문서의 청크 그룹들을 모은 직후 그 안에서만 실행된다. 서로 다른
  문서·소스에 걸친 중복(예: Slack 대화 + 그걸 정리한 회의록 문서, 로드맵 원래 예시)은 함수
  자체는 재사용 가능하지만 자동 연결은 안 되어 있다 — 이건 한 번의 파이프라인 실행 스트림
  바깥의 문제라, "지금까지 쌓인 Knowledge Base 전체를 훑어 병합"하는 별도의 크로스-소스
  재조정(reconciliation) 잡 설계가 필요하다(스코프 성격이 1단계/4단계-4와 비슷 - 백그라운드
  워커/큐 설계가 선행되어야 자연스럽다).
- **`supersedes` 자동 채우기는 `PostgresStore` 전용** — `resolve_supersedes()`가 이전 객체를
  `PipelineStore.list_prior_knowledge_objects()`(→ `PostgresStore.list_knowledge_objects_by_source()`)
  로 조회한다. `--okf-dir`만 쓰고 `--no-store`인 실행에는 이전 버전을 조회할 방법이 없어
  연결되어 있지 않다 - OKF 전용으로 쓰려면 `OKFWriter` 쪽에도 "이 source_id의 기존
  객체를 읽어오는" 조회 경로가 필요하다.
- **묶음(grouping)/병합(dedup)/유사도(supersedes) 임계값은 모두 설정 가능하지만 라이브
  검증은 못함** — `KF_EXTRACTION_CHUNK_GROUP_SIZE`/`KF_DEDUP_SIMILARITY_THRESHOLD`/
  `KF_SUPERSEDES_SIMILARITY_THRESHOLD` 모두 유닛 테스트(mock 기반)로만 검증됨. 특히
  `group_size > 1`일 때 실제 LLM이 여러 청크를 하나의 응답으로 잘 요약/추출하는지, 임계값
  0.9/0.8이 실전 텍스트에 적절한지는 실제 API 키로 확인이 필요하다.

### 6단계 — 문서 전체 요약 (Document Summary) — 후순위, 미착수

`KnowledgeObject.summary`(각 객체 자체의 요약)와 별개로, 문서 전체를 아우르는 요약이 아직 없다
([`knowledge-object-schema.md`](knowledge-object-schema.md) 참고 — `summary` 필드는 "이
구절의 요약이지 문서 전체가 아니다"라고 명시돼 있음). 설계 방향은 정해졌지만 우선순위상
뒤로 미룬다:

- **저장 위치**: `resources/{source_id}.yaml`(OKF 리소스 파일)에 `summary` 필드 1개만 추가.
  문서당 1회 계산·1회 저장 — `objects/{type}/{ko_id}.yaml` 각각에 중복 저장하지 않는다(KO가
  많은 문서에서 중복 비대화 방지).
- **입력 설계 주의 — "elements 전체 텍스트 이어붙이기"는 채택하지 않는다**: chunker
  (`chunking/adaptive.py`)가 `max_tokens_per_chunk=4000`(안전마진 적용 시 실질 3,200)로 문서를
  쪼개는 이유 자체가 "문서를 한 번에 LLM에 못 넣는다"는 전제이고, Document 크기에 상한이
  없어 대용량 PDF/긴 트랜스크립트는 얼마든지 커질 수 있다. 컨텍스트 윈도우 초과 시
  `anthropic.BadRequestError`가 발생하는데 `@anthropic_retry()`의 재시도 대상
  (`RateLimitError`/`APIConnectionError`/`InternalServerError`)에 없어 재시도로 해결되지
  않고, 문서 단위 요약 호출을 그룹 단위 추출처럼 try/except로 격리하지 않으면 요약 실패가
  문서 전체 처리를 중단시킬 위험도 있다.
- **채택할 설계 — map-reduce**: chunk-group별로 이미 만들어지는 `ExtractionResult.summary`
  (`extraction/schemas.py`)를 모아(map은 이미 존재) 그 요약들만 다시 한번 LLM으로 요약
  (reduce) — 원문 전체가 아니라 요약들의 요약이라 입력 크기가 훨씬 작고 예측 가능하다.
  `pipeline.py`의 그룹 병렬 추출·병합(174~182행 부근) 직후, `write_resource` 호출 전에
  reduce 단계를 넣고, 그룹 단위 추출과 동일하게 try/except로 감싸 실패해도 나머지 파이프라인
  (리소스/오브젝트 쓰기)은 계속 진행되도록 한다.
- **설정**: `document_summary_enabled: bool = False` 같은 `KF_*` 옵트인 플래그로 시작(기존
  `KF_DEDUP_SIMILARITY_THRESHOLD` 등과 같은 관례).

**한 줄 정의(향후 아키텍처 문서화 시 재사용)**: "Multi-source SaaS 및 File 데이터를 수집하고,
source별 semantic context를 복원한 뒤 adaptive chunking과 knowledge extraction을 수행하여
Knowledge Base를 구축하는 ingestion pipeline."
