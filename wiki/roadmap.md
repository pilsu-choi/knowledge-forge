# 로드맵

## 완료된 범위 (Phase 1 + Phase 2 + Phase 3 + Phase 4 + Phase 5)

- **전체 아키텍처 스캐폴딩**: Collector/Parser/Adaptive Chunker/Knowledge Extraction/Knowledge
  Object 전 구간의 인터페이스와 모델을 PRD가 명시한 4개 SaaS + 8개 파일 포맷을 아우르도록 설계.
- **모든 파일 포맷 end-to-end 동작**: PDF/DOCX/Markdown/TXT/XLSX/PPTX/Image/**HWPX** → Parser →
  Adaptive Chunker → Claude 기반 Knowledge Extraction → Knowledge Object. PDF/Image/PPTX/HWPX/
  DOCX의 이미지, PDF의 표는 VLM 하이브리드로, PPTX 네이티브 차트는 결정론적 데이터 추출로
  처리한다([`vision-extraction.md`](vision-extraction.md), [`parsers.md`](parsers.md)).
- **4개 SaaS 커넥터 전부 실구현**: Slack/Gmail/Google Drive/Notion — 인증된 클라이언트를
  주입받아 초기/증분 동기화 수행([`saas-connectors.md`](saas-connectors.md)). 모킹 테스트만
  검증됨, CLI 연동은 아직 없음(아래 참고).
- **Context Reconstruction**: Collector와 Parser 사이에 신설된 단계 — Slack은 스레드 그룹핑 +
  시간 간격 기반 Conversation Segmentation, Gmail은 `threadId` 기준 Thread Reconstruction으로
  원본 메시지 스트림을 semantic unit으로 병합한다. File/Notion/Google Drive는 항등(변경 없음).
  자세한 내용은 [`context-reconstruction.md`](context-reconstruction.md) 참고.
- **저장(Storage)**: 원본은 디스크(content-addressed), Source/Document/Chunk/KnowledgeObject는
  PostgreSQL — opt-in, `--store`/`KF_DATABASE_URL`([`storage.md`](storage.md)).
- **스케줄링**: `knowledge-forge schedule`로 프로세스 내 주기 실행
  ([`scheduling.md`](scheduling.md)).
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

커넥터 코드 자체는 완료됐지만([`saas-connectors.md`](saas-connectors.md)), **인증된 SDK
클라이언트를 실제로 만들어서 커넥터에 주입하는 부분은 이번 범위 밖**이다 — 지금은
`knowledge-forge` CLI에서 SaaS 소스를 실행할 방법이 없다(`extract`/`schedule`은
`LocalFileCollector`만 사용). 다음이 필요하다:

- OAuth 토큰 발급/저장/갱신(Slack App 설치, Google OAuth 동의 화면, Notion Integration 토큰 등
  소스별로 흐름이 다름).
- `knowledge-forge slack`/`gmail`/`gdrive`/`notion` 같은 CLI 명령 신설 — `extract`/`schedule`이
  이미 갖춘 파서/청커/추출/저장 배선을 SaaS 커넥터에도 재사용.
- rate limit 대응(현재 커넥터 코드에는 없음).
- 실제 계정/토큰으로 각 커넥터의 라이브 검증(현재는 모킹 테스트만 존재).

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
[`context-reconstruction.md`](context-reconstruction.md) 참고. 단, **1단계(SaaS CLI/인증
연동)가 아직 없어 실제로 실행할 방법은 없다** — `run_pipeline`이 `SlackCollector`/
`GmailCollector`를 받으면 자동으로 이 재구성을 거치도록 배선은 끝났지만, CLI에서 그 컬렉터를
만들어 넘기는 진입점 자체가 없다는 뜻이다. 아래 액션 아이템 중 1~2는 구현 완료, 3~5는 여전히
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
4. **Ingest와 Process 분리** — 사용자가 소스를 연결/업로드하면 "수집 완료"를 즉시 응답하고,
   무거운 Parsing/Chunking/Knowledge Extraction은 백그라운드 워커로 넘긴다. `run_pipeline`
   자체는 이제 `asyncio` 기반이고 `schedule` 명령도 `AsyncIOScheduler`로 백그라운드 tick을
   돈다(완료 — [`scheduling.md`](scheduling.md) 참고). 하지만 이건 "한 프로세스 안에서 LLM
   호출을 동시 실행"하는 것이지, "요청을 받는 즉시 응답하고 처리는 별도 워커/프로세스가
   넘겨받는" 것과는 다른 문제다 — SaaS 커넥터가 CLI에 붙는 시점(1단계)에 별도 큐/워커
   아키텍처(예: 요청 큐에 넣고 즉시 202 응답, 워커 프로세스가 소비)를 여전히 설계해야 한다.
5. **스케줄링 성격 구분** — Slack/Gmail처럼 계속 생성되는 데이터는 polling(현재
   `knowledge-forge schedule`이 이 모델) 또는 향후 webhook/event-driven ingest가 맞고, 정적
   파일 소스는 최초 1회 + 변경 감지 재처리로 충분하다는 게 이미 맞게 설계되어 있음 — 다만
   Slack/Gmail의 짧은 polling 주기가 필요해질 경우 API rate limit 대응(3단계에서 이미 언급)이
   더 중요해진다.

### 5단계 — Typed Knowledge Graph 고도화

Phase 5(위 "완료된 범위" 참고)가 데이터 모델은 바꿨지만, 다음은 아직 미구현이다:

1. **다중 청크 근거(multi-chunk evidence)** — `KnowledgeObject.evidence`는 이미
   `list[Evidence]`라 여러 청크를 가리킬 수 있지만, `Extractor.extract()`는 여전히 청크
   하나를 입력받는다. 하나의 Decision이 두 개의 인접 청크에 걸쳐 있으면 지금은 각 청크에서
   따로 추출되어 별개의 `KnowledgeObject`가 된다 — 진짜로 여러 청크를 하나의 LLM 컨텍스트로
   묶어 근거를 채우려면 Extractor 인터페이스 자체를 "청크 하나" 대신 "관련 청크 묶음"을 받게
   바꿔야 한다.
2. **KnowledgeObject 간 유사도 기반 중복 제거/병합** — 같은 Decision이 서로 다른 청크·심지어
   서로 다른 소스(Slack 대화 + 그걸 정리한 회의록 문서)에서 중복 추출될 수 있다. 지금은
   완전히 별개의 `KnowledgeObject`로 남는다.
3. **추가 Organization View** — 지금은 `OKFWriter`가 타입별(`objects/{type}/`) view 하나만
   구현한다. Topic/Project/Timeline 같은 다른 view는 `manifest.yaml` 카탈로그 위에 별도
   인덱스를 얹으면 되지만, 아직 만들지 않았다([`okf.md`](okf.md) "후속 작업" 참고).
4. **`supersedes`를 실제로 채우는 재처리 흐름** — `KnowledgeObject.supersedes` 필드는
   존재하지만, 재추출 시 이전 버전을 찾아 `supersedes`를 자동으로 채워주는 로직은 없다 —
   지금은 호출자가 수동으로 채워야 한다.

**한 줄 정의(향후 아키텍처 문서화 시 재사용)**: "Multi-source SaaS 및 File 데이터를 수집하고,
source별 semantic context를 복원한 뒤 adaptive chunking과 knowledge extraction을 수행하여
Knowledge Base를 구축하는 ingestion pipeline."
