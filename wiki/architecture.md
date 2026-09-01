# 아키텍처

Knowledge Forge는 다양한 소스의 원본 데이터를 구조와 의미를 보존한 **Knowledge Object**로
변환하는 파이프라인이다. 각 단계는 독립된 인터페이스(ABC)로 분리되어 있어, 특정 소스나 포맷을
추가할 때 다른 단계를 건드릴 필요가 없다.

## 파이프라인 개요

```mermaid
flowchart TD
    subgraph Sources["데이터 소스"]
        F[Local File / SaaS]
    end

    F --> C["Collector\n(initial_sync / incremental_sync)"]
    C -->|RawItem| CR["Context Reconstruction\n(Slack: 대화 세그멘테이션,\nGmail: 스레드 병합,\nFile/Notion/GDrive: 항등)"]
    CR -->|RawItem| P["Parser\n(registry가 확장자/URI 스킴별로 디스패치,\nPDF/Image/PPTX는 VLM 하이브리드)"]
    P -->|Document| CH["Adaptive Chunker\n(Document→Chapter→Section→...)"]
    CH -->|Chunk| E["Knowledge Extraction\n(ClaudeExtractor / OpenRouterExtractor)"]
    E -->|ExtractionOutcome| KO[(typed KnowledgeObjects)]
    E -->|ExtractionOutcome| REL[(Relations - KO 간 typed edge)]

    C -. "knowledge-forge schedule" 사용 시\n일정 간격으로 반복 호출 .-> C
    C -.->|record_source| ST[(PipelineStore\nPostgres + 디스크)]
    P -.->|record_document| ST
    CH -.->|record_chunks| ST
    E -.->|record_knowledge_objects / record_relations| ST
    P -.->|write_resource| OKF[(OKF 파일 트리\nmanifest.yaml)]
    E -.->|write_objects / write_relations| OKF

    style KO fill:#2b6,stroke:#164,color:#fff
    style REL fill:#2b6,stroke:#164,color:#fff
    style ST fill:#458,stroke:#124,color:#fff
    style OKF fill:#458,stroke:#124,color:#fff
```

각 화살표 라벨이 단계 간에 오가는 자료형이다. `RawItem → Document → Chunk → ExtractionOutcome`
순으로 갈수록 더 구조화되고, 동시에 원본으로 되돌아갈 수 있는 참조(provenance)를 계속 유지한다.
`Chunk`는 한 개의 `KnowledgeObject`로 고정 대응되지 않는다 — 아래 "설계 결정: Chunk와
KnowledgeObject는 1:1이 아니다" 참고.

점선은 옵션 경로다: `PipelineStore`(저장, [`storage.md`](storage.md) 참고)는 `--store`/
`KF_DATABASE_URL`이 설정된 경우에만 각 단계 산출물을 Postgres+디스크에 기록하며, 없어도 파이프라인은
동일하게 동작한다(출력만 생성). `OKFWriter`(`--okf-dir`, [`okf.md`](okf.md) 참고)는 같은 산출물을
파일 기반 Knowledge Base 트리로도 기록할 수 있는 별도의 옵션 경로다 — `store`와 동시에 켜도 되고,
독립적으로도 켤 수 있다. `knowledge-forge schedule`([`scheduling.md`](scheduling.md) 참고)은
`extract`와 동일한 파이프라인을 한 프로세스 안에서 반복 실행하는 대안적 진입점이다.

## 컴포넌트별 책임

### Collector (`src/knowledge_forge/collectors/`)

- 원본 데이터를 있는 그대로 가져와 `RawItem`(uri, content, content_hash, modified_at, metadata,
  deleted)으로 통일한다.
- `Collector` ABC는 `initial_sync()` / `incremental_sync(cursor)` / `get_cursor()` 세 메서드로
  구성되며, 파일시스템 순회(File)와 페이지네이션 API 폴링(SaaS)을 동일한 형태로 다룰 수 있게
  설계했다. 변경 감지는 `content_hash`, 삭제 감지는 `RawItem.deleted=True`로 통일적으로 표현한다.
- `LocalFileCollector`는 실제로 동작하며, JSON 매니페스트(경로→해시/mtime)를 저장해 증분
  동기화 시 변경/신규/삭제를 구분한다. `uri`는 항상 `file://{절대경로}` 형태로 원본 위치를
  보존한다.
- Slack/Gmail/GoogleDrive/Notion 커넥터는 실제 SDK 연동으로 동작한다 — 다만 CLI에서 실행할 방법은
  아직 없다. 자세한 내용은 [`saas-connectors.md`](saas-connectors.md)와 [`roadmap.md`](roadmap.md)
  참고.

### Context Reconstruction (`src/knowledge_forge/context/`)

- Collector가 만든 `RawItem` 하나가 항상 독립된 의미 단위인 건 아니다 — 파일은 맞지만, Slack
  메시지 하나나 Gmail 이메일 하나는 그 자체로 의미가 불완전할 수 있다. `ContextReconstructor` ABC의
  `reconstruct(items) -> Iterator[RawItem]`가 Collector와 Parser 사이에서 원본 아이템 스트림을
  재구성해 semantic unit으로 병합한다.
- `default_reconstructor_for(source_type)`가 소스별 기본 구현을 고른다: File/Notion/Google
  Drive는 `NoopReconstructor`(항등, 변경 없음), Slack은 `SlackConversationReconstructor`(스레드
  그룹핑 + 시간 간격 기반 Conversation Segmentation), Gmail은 `GmailThreadReconstructor`
  (`threadId` 기준 병합)다. `pipeline.run_pipeline`이 별도 인자를 안 주면 이 기본값을 쓴다.
- 병합된 `RawItem`은 URI에 파일 확장자가 없으므로(`slack://...`, `gmail://thread/...`),
  `ParserRegistry`가 확장자뿐 아니라 URI 스킴으로도 디스패치할 수 있게 확장되어 있고, 새
  `ConversationParser`(`parsers/conversation.py`)가 두 스킴을 모두 처리한다.
- 자세한 설계와 예시는 [`context-reconstruction.md`](context-reconstruction.md) 참고.

### Parser (`src/knowledge_forge/parsers/`)

- `Parser` ABC의 `async def parse(item, source_ref) -> Document` 하나로 모든 포맷을 공통
  `Document` 구조로 변환한다. `ParserRegistry`가 파일 확장자(또는 Context Reconstruction이
  만든 아이템의 URI 스킴)를 보고 알맞은 Parser를 찾아 디스패치한다.
- `Document.elements`는 `Heading` / `TextBlock` / `TableElement` / `ListElement` / `ImageElement`의
  판별 유니온(discriminated union)이며, 각 요소는 `element_id`, `page`(가능한 경우), `bbox`(페이지 내
  위치, 현재는 PDF만)를 갖는다. `TableElement`/`ImageElement`는 추가로 `extraction_method`
  (`"rule_based"` | `"vlm"` | `None`)를 가져 어느 경로로 만들어졌는지 추적할 수 있다.
- PDF/DOCX/Markdown/TXT/XLSX/PPTX/Image/HWPX 전부 실제 동작한다(HWPX는 `python-hwpx` 기반,
  실제 한컴 생성 파일로는 미검증 — [`parsers.md`](parsers.md) 참고).
- **PDF/DOCX/PPTX/HWPX/Image는 순수 규칙 기반이 아니라 VLM 하이브리드다**: 일반 텍스트/네이티브
  표(DOCX/PPTX/HWPX)·PPTX 네이티브 차트는 여전히 결정론적으로 추출하지만, PDF의 표/이미지와
  모든 포맷의 임베드 이미지·독립 이미지 파일은 항상 Vision Provider(기본 Claude vision, 아래
  참고)로 보낸다. 한 문서 안의 표/이미지 여러 개는 `asyncio.gather`로 동시에 처리되며(요소
  순서/`element_id`는 그대로 유지), 동시 요청 수는 `KF_MAX_CONCURRENT_LLM_REQUESTS`로 제한된다.
  설계 근거와 상세 동작은 [`vision-extraction.md`](vision-extraction.md) 참고.
- 포맷별 라이브러리와 라이선스 근거는 [`parsers.md`](parsers.md) 참고.

### Adaptive Chunker (`src/knowledge_forge/chunking/`)

- `Chunker` ABC의 `chunk(document) -> list[Chunk]`. `Document.elements`를 Chapter/Section/Paragraph
  트리로 구성한 뒤, 토큰 예산을 넘을 때만 더 작은 단위(Sentence/Line)로 내려간다.
- 자세한 알고리즘과 설계 원칙은 [`adaptive-chunking.md`](adaptive-chunking.md) 참고.

### Knowledge Extraction (`src/knowledge_forge/extraction/`)

- `Extractor` ABC의 `async def extract(chunks: list[Chunk], document, source) -> ExtractionOutcome`.
  `chunks`는 항상 비어 있지 않은 리스트다 — `extraction/grouping.py::group_chunks()`가 한
  문서의 청크들을 `KF_EXTRACTION_CHUNK_GROUP_SIZE`(기본 1, 즉 기존과 동일하게 청크 하나당 호출
  한 번)개씩 묶어 넘긴다. `ExtractionOutcome`은 `objects: list[KnowledgeObject]`와
  `relations: list[Relation]` 두 필드다 — 하나의 `Chunk`가 하나의 `KnowledgeObject`로 고정
  대응되지 않는다(아래 설계 결정 참고).
- 기본 구현 `ClaudeExtractor`가 Claude LLM을 호출해 `ExtractionResult`(entities/relations/typed
  items/topics/document_type)를 얻고, 프로바이더 공용 로직인
  `extraction/outcome.py::build_extraction_outcome()`이 이를 `ExtractionOutcome`으로 변환한다 —
  엔티티마다 하나씩 `ENTITY` 타입 `KnowledgeObject`를, 각 타입 항목(item)마다 하나씩
  Fact/Event/Decision/ActionItem/... 타입 `KnowledgeObject`를 만들고, 엔티티-엔티티 관계·
  item-엔티티 mentions·item 간 caused_by/relates_to를 각각 `Relation`으로 만든다.
- 스키마와 프롬프트 설계는 [`knowledge-object-schema.md`](knowledge-object-schema.md) 참고.
- **동시 실행**: `pipeline.run_pipeline`은 한 문서의 청크 그룹들을 `asyncio.gather`로 동시에
  추출한다. `ClaudeExtractor`/`OpenRouterExtractor`(그리고 두 Vision 구현) 각각이 자체
  `asyncio.Semaphore`로 실제 동시 API 요청 수를 제한하므로(기본 5,
  `KF_MAX_CONCURRENT_LLM_REQUESTS`), 파이프라인 쪽은 동시성 상한을 따로 관리할 필요가 없다.
  청크 그룹 하나가 `KnowledgeExtractionError`로 실패해도 나머지 형제 그룹은 그대로 처리·
  저장된다.
- **같은 문서 안 중복 제거 + 재추출 버전 관리**: 한 문서의 모든 청크 그룹이 추출을 마치면,
  `dedup.py::merge_duplicates()`가 타입이 같고 `summary`가 유사한(기본 임계값
  `KF_DEDUP_SIMILARITY_THRESHOLD=0.9`) 객체들을 하나(가장 먼저 추출된 것)로 합치고
  `DUPLICATE_OF` `Relation`으로 감사 기록을 남긴다. `store`가 주어진 경우(재추출 상황) 이어서
  `extraction/versioning.py::resolve_supersedes()`가 같은 `source_id`의 이전 실행 객체들과
  타입+텍스트 유사도(기본 `KF_SUPERSEDES_SIMILARITY_THRESHOLD=0.8`)를 비교해 일치하는 새
  객체의 `supersedes`/`version`을 자동으로 채우고 `SUPERSEDES` `Relation`을 추가한다. 두
  단계 모두 문서 하나의 청크 그룹 전체를 한 번에 봐야 하므로, `run_pipeline`은 문서당 병합된
  `ExtractionOutcome` 하나를 yield한다(이전처럼 청크(그룹)당 하나가 아니다).
- **LLM Provider 추상화**: `extraction/factory.py`의 `get_extractor()`와
  `vision/factory.py`의 `get_vision_extractor()`가 `KF_LLM_PROVIDER`(`anthropic`(기본)|
  `openrouter`)에 따라 `ClaudeExtractor`/`VisionExtractor` 또는 `OpenRouterExtractor`/
  `OpenRouterVisionExtractor`를 고른다 — 두 구현 모두 같은 ABC/인터페이스를 만족해 CLI·파서
  쪽 코드는 프로바이더가 바뀌어도 그대로다. 자세한 내용은 [`llm-providers.md`](llm-providers.md)
  참고.
- `extraction/auth.py`가 활성 프로바이더에 맞는 자격 증명(Anthropic이면
  `ANTHROPIC_API_KEY`/`ant auth login`, OpenRouter면 `OPENROUTER_API_KEY`)이 존재하는지 CLI
  시작 시점에 미리 점검한다 — Knowledge Extraction과 Vision 추출(아래) 둘 다 이 자격 증명을
  공유한다.

### Storage (`src/knowledge_forge/storage/`) — 선택적

- `PipelineStore`가 파이프라인의 각 단계 산출물을 저장하는 파사드다: 원본 바이트는 디스크에
  (`RawBlobStore`), `Source`/`Document`/`Chunk`/`KnowledgeObject`/`Relation`은 PostgreSQL에
  (`PostgresStore`). `pipeline.run_pipeline(..., store=...)`가 `None`이면(기본값) Phase 1과
  동일하게 저장 없이 동작한다.
- 상세 스키마와 사용법은 [`storage.md`](storage.md) 참고.

### OKF — 파일 기반 Knowledge Base (`src/knowledge_forge/okf/`) — 선택적

- `OKFWriter`가 `PipelineStore`와는 독립적인 두 번째 옵션 출력 경로다: 같은 `Source`/`Document`/
  `KnowledgeObject`/`Relation`을 `manifest.yaml` + `resources/`/`objects/{type}/`/`relations/`
  디렉터리 트리로 기록한다 — 데이터베이스 없이도 사람이 읽고 git으로 diff할 수 있는 Knowledge
  Base 형태다. `manifest.yaml`은 knowledge 자체를 담지 않는 카탈로그(각 레코드의 id/type/경로
  목록)로, 트리를 스캔해 재생성되므로 여러 번의 파이프라인 실행(증분 동기화 등)에 걸쳐 안전하게
  누적된다. `write_views()`가 같은 스캔 방식으로 `views/by_topic.yaml`/`by_document.yaml`/
  `timeline.yaml`(타입별 view 외의 Topic/Document/Timeline 조회 관점, 레코드 파일 자체는
  건드리지 않는 인덱스)을 만든다. 자세한 설계는 [`okf.md`](okf.md) 참고.

### Scheduling (`src/knowledge_forge/scheduling/`) — 선택적

- `knowledge-forge extract`는 1회성 실행이고, `knowledge-forge schedule`은 동일한 파이프라인을
  `APScheduler`(`AsyncIOScheduler`)로 일정 간격마다 asyncio 이벤트 루프 위의 백그라운드 job으로
  반복 실행하는 장기 실행 프로세스다. 상세 동작은 [`scheduling.md`](scheduling.md) 참고.

### API 서버 + Job Queue/Worker (`src/knowledge_forge/api/`, `worker.py`) — 선택적

- 위 두 실행 모드(`extract`/`schedule`)는 모두 호출 프로세스 안에서 파이프라인이 끝까지
  블로킹 실행된다. `knowledge-forge serve`(FastAPI)는 이 프로젝트 최초의 HTTP 경계로,
  `POST /ingest`가 요청을 `jobs` 테이블(Postgres, `FOR UPDATE SKIP LOCKED` 폴링 큐)에 넣고
  즉시 `202`+job_id를 응답한다 — 실제 파이프라인 실행은 별도 프로세스인 `knowledge-forge
  worker`가 큐를 폴링해 넘겨받는다("Ingest와 Process 분리", `roadmap.md` 4단계).
- `jobs.job_type`이 payload 모양을 구분하는 확장점이다 — v1은 이미 동작하는
  `LocalFileCollector` 경로를 쓰는 `local_file_ingest` 하나뿐이고, SaaS 소스는 CLI/인증 연동
  (로드맵 1단계) 이후 새 job_type으로 추가된다. 자세한 내용은 [`api.md`](api.md) 참고.

## 확장 포인트

새로운 SaaS 커넥터를 추가하려면:
1. `collectors/saas/`에 `SaaSCollector`를 상속한 새 클래스를 만들고 `initial_sync`/
   `incremental_sync`/`get_cursor`를 구현한다.
2. 다른 어떤 단계도 수정할 필요가 없다 — Parser/Chunker/Extraction은 `RawItem`/`Document`라는
   공통 계약에만 의존한다.

새로운 파일 포맷을 추가하려면:
1. `parsers/`에 `Parser`를 상속한 클래스를 만들고 `supported_extensions`와 `parse()`를 구현한다.
2. `parsers/registry.py`의 `default_registry()`에 등록한다.
3. Collector/Chunker/Extraction은 그대로 재사용된다.

이 ABC + 레지스트리 패턴 덕분에 PRD가 요구하는 4개 SaaS + 8개 파일 포맷 전체를, 이번 MVP처럼
일부만 실제 구현하고 나머지는 인터페이스만 정의해 두는 점진적 확장이 자연스럽게 가능하다.

## 설계 결정: Chunk와 KnowledgeObject는 1:1이 아니다

초기 구현은 `extract(chunk, ...) -> KnowledgeObject` 하나로, 청크 하나가 항상 정확히 하나의
`KnowledgeObject`(고정된 summary/facts/entities/relations 필드를 가진 단일 bag)를 낳는다고
가정했다. 이 가정은 두 방향 모두에서 깨진다:

- 청크 하나가 여러 개의 독립적인 knowledge를 담을 수 있다. 예: Slack 대화 한 덩어리가
  `{type: Issue, "QA 미완료"}`, `{type: Decision, "배포를 월요일로 연기"}`,
  `{type: ActionItem, "월요일 10시 배포"}` 세 개의 독립적으로 참조 가능한 객체를 포함한다.
- 반대로 하나의 knowledge(예: 하나의 Decision)가 여러 청크에 걸쳐 근거를 가질 수 있다.

그래서 `Extractor.extract()`는 `KnowledgeObject` 하나가 아니라 `ExtractionOutcome`
(`objects: list[KnowledgeObject]`, `relations: list[Relation]`)을 반환한다. `KnowledgeObject`는
`type`(`KnowledgeObjectType`: Entity/Fact/Event/Decision/ActionItem/Requirement/Policy/Issue/
Concept)으로 구분되는 typed envelope이고, 서로 다른 `KnowledgeObject`를 연결하는 관계
(Decision이 어떤 Issue 때문인지, 어떤 Entity를 언급하는지 등)는 `KnowledgeObject`에 내장된
텍스트 triple이 아니라 별도의 `Relation` 레코드(id로 두 `KnowledgeObject`를 잇는 typed edge)로
표현한다. `models/knowledge_object.py`/`models/relation.py`/`extraction/outcome.py`의 설계
노트에 더 자세한 근거가 있다.

`Extractor.extract()`는 이제 청크 하나가 아니라 청크 묶음(`chunks: list[Chunk]`)을 받는다 —
`extraction/grouping.py::group_chunks()`가 한 문서의 청크들을 `KF_EXTRACTION_CHUNK_GROUP_SIZE`
개씩(기본 1) 순서대로 묶어 파이프라인이 넘긴다. 기본값 1은 기존과 정확히 같은 동작(청크당 호출
한 번)을 유지하기 위한 안전한 기본값이고, 이 값을 2 이상으로 올리면 인접한 여러 청크가 한
LLM 호출로 묶여 `KnowledgeObject.evidence`가 실제로 여러 청크를 가리키게 된다(`extraction/
outcome.py::build_extraction_outcome()`이 묶음의 모든 청크로 `evidence`를 채운다). 묶음
전략은 지금은 순서 기반 윈도잉일 뿐 의미 기반(유사도 등)은 아니다 — 인접성이 "같은 claim에
속할 가능성이 높다"는 값싼 근사치라는 전제.

## 설계 결정: Provenance는 참조(ref)로, 전체 객체 임베드 아님

`KnowledgeObject.source`/`document`는 `Source`/`Document` 전체를 담지 않고 `SourceRef`/
`DocumentRef`라는 가벼운 참조(id + 식별에 필요한 최소 필드)만 담는다. 어느 `Chunk`(들)가 이
`KnowledgeObject`를 뒷받침하는지는 `evidence: list[Evidence]`가 각 청크의 id + 위치 정보만
담아 표현한다(위 "Chunk와 KnowledgeObject는 1:1이 아니다" 참고).

**이유**: 한 `Document`는 보통 여러 `Chunk`로 쪼개지고, 각 `Chunk`는 여러 `KnowledgeObject`를
만들 수 있다. 만약 매 `KnowledgeObject`가 원본 `Document` 전체(모든 elements 포함)를 껴안고 있다면,
문서 하나가 100개 청크로 쪼개졌을 때 동일한 문서 데이터가 100번 중복 저장된다. 참조 방식을
쓰면 `Source`/`Document`/`Chunk` 레코드는 파이프라인 실행 결과물 안에서 id로 한 번만 저장되고,
`KnowledgeObject`는 그 id 체인(`source_id → document_id → evidence[].chunk_id →
knowledge_object_id`)만 유지하면 된다. PRD가 요구하는 "모든 Knowledge Object는 Provenance를
유지한다"는 조건은 이 id 체인으로 충분히 만족된다.

## 설계 결정: Hierarchy는 Knowledge Base의 유일한 조직 원리가 아니다

Document 파싱/청킹 단계(`Document → Chapter → Section → Paragraph → ...`)는 여전히 계층
구조를 적극 활용한다 — 문서에서는 그 계층 자체가 의미 있는 메타데이터이기 때문이다. 하지만
그 계층은 **파일 소스의 내부 구조를 다루는 방식**이지, Knowledge Base 전체를 조직하는 유일한
원리로 취급하지 않는다:

- Slack/Gmail은 Document의 Chapter/Section이 아니라 Context Reconstruction이 만든
  Episode/Thread 단위로 처리된다([`context-reconstruction.md`](context-reconstruction.md)).
- `KnowledgeObject`끼리는 `Relation`이라는 그래프 구조로 연결된다 — 계층이 아니라 typed edge다.
- `OKFWriter`가 만드는 파일 트리(`objects/{type}/...`)는 여러 조회 관점(view) 중 파일시스템에
  자연스럽게 얹을 수 있는 "타입별" 하나일 뿐, 유일한 organization view는 아니다 —
  `write_views()`가 `manifest.yaml`의 카탈로그(id + type + path) 위에 Topic/Document(Project의
  대용 — 스키마에 별도 Project 개념은 없다)/Timeline 세 view를 별도 인덱스 파일로 추가한다
  ([`okf.md`](okf.md) 참고). 레코드 파일 포맷 자체는 바뀌지 않는다 — view는 항상 기존
  `objects/`를 다시 읽어 만드는 파생 인덱스다.

## 설계 결정: 중복 제거와 재추출 버전 관리는 같은 유사도 원리를 공유한다

두 문제가 형태는 비슷하지만 축이 다르다 — "이 새 객체가 이미 있는 다른 객체와 같은
claim인가"라는 질문을 (a) **같은 실행 안에서** 물으면 중복 제거(`dedup.py`), (b) **이전 실행
결과와** 물으면 재추출 버전 관리(`extraction/versioning.py`)다. 둘 다 `similarity.py`의
`text_similarity()`(embedding이나 Vector DB 없이 `difflib.SequenceMatcher`로 충분히 짧은
문장 단위 텍스트를 비교 — Vector DB가 제외 범위인 이유와 같은 스코프 판단)를 같은 방식으로
쓴다:

- **`dedup.py::merge_duplicates()`**: 타입이 같고 `summary`가 유사한 객체들을 하나(가장 먼저
  추출된 것)로 합친다. id는 KnowledgeObject의 내용에서 결정론적으로 파생되므로(`ids.py`),
  같은 chunk bundle에서 나온 객체는 애초에 같은 id를 가져 자연 upsert되지만, 서로 다른 청크
  (묶음)에서 독립적으로 추출된 "같은" claim은 다른 id를 가지므로 텍스트 유사도로만 잡아낼 수
  있다. `Relation`을 하드 삭제하지 않고 병합에 참여한 모든 관계를 canonical id로 재작성한
  뒤, 병합됐다는 사실 자체는 `DUPLICATE_OF` `Relation`으로 남긴다 — Postgres FK
  참조 무결성을 깨지 않으면서(관계가 가리키는 객체는 항상 결과 집합 안에 존재) 감사 추적을
  잃지 않기 위해서다.
- **`extraction/versioning.py::resolve_supersedes()`**: `document_id`는
  `stable_id("document", uri, content_hash)`라 소스 내용이 바뀌면(재추출) 항상 새 id가
  나온다 — 즉 재추출은 이전 실행의 id를 절대 재사용하지 않으므로 자연 upsert로는 버전 연결이
  안 된다. 대신 내용 변경에도 안정적인 `source_id`(uri만으로 결정)를 축으로, 같은 소스의
  이전 객체들 중 타입+텍스트가 유사한 것을 찾아 새 객체의 `supersedes`/`version`을 채우고
  `SUPERSEDES` `Relation`을 추가한다 — 이전 버전 객체는 지우지 않고(`PostgresStore`에 그대로
  남아 다음 `list_knowledge_objects_by_source()` 조회의 후보 풀이 된다) 새 버전이 그것을
  가리키는 방식이라 두 버전 모두 id로 계속 조회 가능하다.

두 패스 모두 `pipeline.run_pipeline`이 한 문서의 모든 청크 그룹을 `asyncio.gather`로 모은
직후, 저장/OKF 기록/yield 전에 실행된다 — 문서 하나를 통째로 봐야만 "그룹 A의 객체가 그룹
B의 객체와 중복인가"/"이 새 객체가 이전 실행의 무엇을 대체하는가"를 정확히 판단할 수 있기
때문이다(부분 집합만 보면 놓친다). 이 때문에 `run_pipeline`은 이제 청크(그룹)당 하나가 아니라
문서당 병합된 `ExtractionOutcome` 하나를 yield한다.
