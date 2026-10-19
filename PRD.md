# PRD — Knowledge Forge

> 기준: 2026-09-16, `main` `7ae110f`. 2026-09-01 초판(수집·추출 파이프라인 MVP)을 현재 구현과 목표에 맞춰 개정했다.
> 변경 요지: ① 초판의 MVP 범위(Collector→Parser→Chunker→Extraction)는 완료되었고, ② 초판이 "제외 범위"로 둔 Vector DB·Graph·Search/RAG·Agent가 실제로 구현되어 범위 안으로 들어왔으며, ③ 제품 목표가 "추출 파이프라인"에서 **"사내 지식 → 온톨로지 → 여러 Agent가 소비하는 지식 플랫폼"**(Palantir Foundry형)으로 확장되었다.

## 1. 제품 목적

기업의 흩어진 데이터를 수집해 **문서 구조를 보존한 Semantic Chunk**로 만들고, LLM으로 **typed Knowledge Object 그래프**를 생성한 뒤, 그 위에 **온톨로지 층**을 세워 **여러 형태의 Agent가 공통으로 소비**하게 한다.

```text
Data Source
    ↓
Collector  ─────────────────┐
    ↓                       │
Context Reconstruction      │  Ingestion & Extraction (완료)
    ↓                       │
Parser                      │
    ↓                       │
Adaptive Chunker            │
    ↓                       │
Knowledge Extraction ───────┘
    ↓
Knowledge Object + Relation (typed graph)
    ↓
Ontology Layer ─────────────┐  정규 엔티티 · 타입별 속성 · 그래프 조회 (진행 예정)
    ↓                       │
Retrieval / MCP ────────────┘
    ↓
소비 Agent (검색 Agent = 첫 번째 소비자, 그 외 N개)
```

### 지향점

* **Foundry형 구조**: 지식 층(온톨로지)이 하나이고, 소비자(Agent·UI·API)는 여러 개다. 검색 Agent는 그중 첫 소비자일 뿐 최종 산출물이 아니다.
* **온프렘 · 오픈소스 LLM**: 대상 모델은 `gpt-oss-120b` 급 오픈소스 모델을 OpenAI 호환 엔드포인트(vLLM 등)로 연결한다. Claude/OpenRouter는 현재 기본값이자 품질 기준선이며, 종속 대상이 아니다.
* **품질과 지연을 동시에**: 품질을 유지하면서 응답 지연을 낮춘다. 지연의 지배 요인은 DB가 아니라 질의당 LLM 호출 횟수와 프리필 크기다.

## 2. 지원 데이터

### SaaS (4종, 실 SDK 연동 구현 완료)

| 소스 | 초기 동기화 | 증분 동기화 | 삭제 감지 |
|---|---|---|---|
| Slack | ✅ | ✅ | ❌ (API 한계) |
| Gmail | ✅ | ✅ (History API) | ✅ |
| Google Drive | ✅ | ✅ (Changes API) | ✅ |
| Notion | ✅ | ✅ | ❌ → 매니페스트 diff |

### File (9종)

`PDF` · `DOCX` · `XLSX` · `PPTX` · `HWPX` · `HTML` · `Markdown` · `TXT` · `Image`

초판 8종에 `HTML`이 추가되었다. 표·이미지·차트는 폴백이 아니라 정책으로 VLM(하이브리드 파싱)을 거친다.

## 3. 핵심 기능

### ① Collector

데이터 소스에서 원본을 수집해 `RawItem`으로 통일한다. Initial/Incremental Sync, 변경·삭제 감지, Source Metadata·원본 URL 보존.

### ② Context Reconstruction

RawItem 하나가 항상 하나의 의미 단위는 아니라는 문제를 해결한다. Slack 대화 세그멘테이션, Gmail 스레드 병합. 나머지 소스는 항등.

### ③ Parser

모든 포맷을 공통 `Document` 구조(Text/Heading/Table/List/Image/Page/Metadata)로 변환한다. 오픈소스 Parser/OCR을 우선 활용하고, 표·이미지·차트는 VLM으로 처리한다.

### ④ Adaptive Chunker

고정 Token 단위가 아닌 **문서 구조 기반 Chunking**.

```text
Document → Chapter → Section → Paragraph → Sentence → Line
```

상위 단위가 Context Window를 초과할 때만 하위로 분할한다.

> **가능한 가장 큰 Semantic Unit을 유지한다.**

### ⑤ Knowledge Extraction

LLM으로 Chunk에서 Knowledge를 추출한다. Entity / Fact / Event / Decision / ActionItem / Requirement / Policy / Issue / Concept과 그 사이의 Relation.

청크 1개 ↔ Knowledge Object 1개가 아니다. 추출 결과는 `ExtractionOutcome`(`objects[]` + `relations[]`)이며, Chunk와 Knowledge Object는 다대다다.

### ⑥ Storage / OKF (선택)

* **PipelineStore**: 원본 바이트는 디스크, 구조화 레코드는 PostgreSQL(+pgvector).
* **OKF**: `manifest.yaml` + 파일 트리로 된, DB 없이도 읽고 git-diff 가능한 Knowledge Base.

둘 다 opt-in이며, 없어도 파이프라인은 동일하게 동작한다(출력만 생성).

### ⑦ Retrieval

결정론적 하이브리드 검색. 질의 의도 분석 → 제목 조회/렉시컬/벡터/메타데이터 필터/멀티홉/문서 전체 읽기를 조합한다. 어떤 검색을 어떤 순서로 돌릴지는 모델이 아니라 검색 코어가 정한다. Query Planning·Routing·RRF 융합·Temporal Search·Text-to-SQL 구현 완료.

### ⑧ Agent Runtime

모델 벤더 중립 도구 호출 루프(`Anthropic 네이티브` | `OpenAI 호환`). 노출 도구: `search` / `read_document` / `sql_search` / 파일시스템 / `write_memory` / `delegate_task`.

### ⑨ Evaluation

grain(`chunk` | `knowledge_object`) 고정 골든셋 자동 생성, retrieval 지표(기본, LLM 0회)와 에이전트 답변 채점(`--e2e`), paired bootstrap A/B 비교.

### ⑩ Ontology Layer — **미구현, 이번 개정의 핵심 신규 범위**

현재 온톨로지는 "타입 enum 9개 + 자유 dict"에 머문다. Foundry형으로 쓰려면 다음이 필요하다.

* **정규 엔티티**: `entities`(정규화 이름 + 타입) 테이블과 `entity_mentions`(엔티티 ↔ KO) 조인. 지금은 엔티티 해소가 없어 같은 인물이 수십 개 객체로 흩어진다.
* **타입별 속성**: decision(내용·결정자·날짜·상태), action_item(담당·기한·상태), event(시점·참여자)부터 실제 속성을 두고 타입별로 검증한다. 현재는 9개 타입 중 8개가 `{"statement": summary}` 하나다.
* **그래프 읽기 경로**: `neighbors(object_id, depth, link_types)` 저장소 API와 이를 노출하는 Agent 도구·HTTP 엔드포인트. 지금은 relations를 쓰기만 하고 읽는 코드가 없다.
* **소비 인터페이스**: `lookup_entity` / `get_object` / `neighbors` / `search` 읽기 전용 MCP 서버. 모든 도구에 `project_id` 범위를 강제한다. 두 번째 소비 Agent가 붙을 수 있는지에 대한 수용 시험이기도 하다.

## 4. Knowledge Object

`KnowledgeObject`는 타입별 최상위 모델이 아니라 **generic typed envelope**다. 타입은 `type`이 구분하고, 타입별 payload는 `content: dict`에 담는다. 객체 간 관계는 객체 안의 텍스트가 아니라 별도 `Relation` 레코드다.

```json
{
  "knowledge_object_id": "ko-...",
  "type": "decision",
  "summary": "배포를 월요일로 연기",
  "content": {},
  "topics": [],
  "document_type": "meeting_notes",
  "source": { "source_id": "..." },
  "document": { "document_id": "...", "title": "..." },
  "evidence": [{ "chunk_id": "...", "document_id": "...", "hierarchy_level": "section", "sequence_index": 3 }],
  "extracted_at": "2026-09-16T00:00:00Z",
  "model_used": "...",
  "pipeline_version": "...",
  "source_hash": "...",
  "version": 1,
  "supersedes": null
}
```

```json
{
  "relation_id": "rel-...",
  "type": "caused_by",
  "subject_id": "ko-decision-...",
  "object_id": "ko-issue-...",
  "label": "원문 술어",
  "evidence": ["chunk-..."]
}
```

모든 Knowledge Object는 id 참조로 Provenance를 유지한다.

```text
Source → Document → Chunk(들) → Knowledge Object
```

## 5. 범위

### 구현 완료

수집(파일 + SaaS 4종) · Context Reconstruction · 파싱 9종(VLM 하이브리드) · Adaptive Chunking · Knowledge Extraction(typed envelope + Relation) · PostgreSQL/디스크 저장 · OKF 파일 트리 · pgvector 임베딩 · 하이브리드 검색 · Agent Runtime · 검색 평가 · API 서버 + Celery 워커 · 주기 실행 · OKF 뷰어 UI(`/ui`).

### 초판의 "제외 범위"에 대한 현재 판정

| 초판 제외 항목 | 현재 |
|---|---|
| Vector DB | **범위 안** — pgvector + HNSW, `--embed`/`embed` 백필 |
| Graph DB | **범위 안** — `KnowledgeObject`(노드) + `Relation`(typed edge). 다만 **읽기 경로 미구현**(⑩) |
| Ontology / OWL | **범위 안으로 승격** — OWL 자체가 아니라 Foundry형 온톨로지 층(⑩). 미구현 |
| Search / RAG | **범위 안** — 하이브리드 검색·플래너·RRF·Text-to-SQL 구현 |
| Agent | **범위 안** — 벤더 중립 Agent Runtime 구현. 소비 Agent는 앞으로 늘어난다 |
| Dashboard | 여전히 제외 — OKF 뷰어(`/ui`)는 탐색용이며 대시보드가 아니다 |
| Chat UI | 여전히 제외 — 소비 표면은 CLI/API/MCP |

### 여전히 제외

Dashboard · Chat UI · OWL/SHACL 형식 온톨로지 · 쓰기(action) 권한을 갖는 Agent · 멀티테넌시 인증·권한 모델.

## 6. 품질 · 지연 요구사항

품질과 지연은 같은 A/B에서 함께 기록한다. 현재 두 축 모두 **목표값이 없고**, 현재 `main`에서 오픈소스 모델로 잰 e2e 수치가 한 건도 없다 — 이것이 첫 번째 할 일이다.

| 요구 | 현재 상태 | 해야 할 것 |
|---|---|---|
| 검색 품질 기준선 | retrieval 지표만 있음. e2e는 삭제된 코드에서 나온 옛 수치뿐 | 질의 임베딩을 고정하고 Claude 1개 + `gpt-oss-120b` 1개로 e2e 측정 |
| 골든셋 신뢰도 | v1은 LLM 생성·사람 미검수(`draft`) | 50~100개 사람 검수 부분집합 |
| 재현성 | 원격 임베딩 흔들림으로 같은 코드도 결과가 달라짐 | 온프렘 임베딩 전환 + 질의 임베딩 고정·재생 |
| 지연 SLO | 없음 | 종단 p50/p95와 첫 토큰까지 시간 목표를 정의하고 게이트에 넣는다 |
| 오픈소스 모델 대응 | 어댑터에 reasoning 필드 처리·`max_tokens` 설정 없음 | 어댑터 보강 후 동일 골든셋으로 재측정 |

## 7. 다음 범위 (우선순위)

세부 항목 번호(M/R/L/S/O)와 근거는 [`wiki/improvement-plan-2026-09-15.md`](wiki/improvement-plan-2026-09-15.md).

1. **측정 기반 확보** — e2e 기준선, 지연 SLO, 임베딩 고정. 이것 없이는 아래 변경의 효과를 판단할 수 없다.
2. **품질 영향 없는 지연 절감 + 오픈소스 대응** — 첫 턴 search 선실행, 프롬프트 프리픽스 축소, 스트리밍, 어댑터 보강(reasoning·`max_tokens`·로컬 엔드포인트 키 검사 완화).
3. **검색 부품 교체** — 온프렘 임베딩 → 리랭커 → 한국어 BM25 → RRF 융합 단위 정정. 임베딩을 먼저 바꿔야 이후 A/B가 흔들리지 않는다.
4. **측정 후 결정** — 제약 보강 LLM 호출의 기본값, 에이전트에 넘길 결과 크기.
5. **온톨로지 층(⑩)** — `embedding_source` 기본값 정정은 즉시, 이후 정규 엔티티 → 타입별 속성 → 그래프 읽기 → MCP 순. 3번과 독립이라 병렬 진행 가능.

## 8. 한 줄 정의

> **Slack, Gmail, Google Drive, Notion 및 다양한 파일을 수집해 구조와 provenance를 보존한 Knowledge Object 그래프로 만들고, 그 위의 온톨로지를 여러 Agent가 공통으로 소비하는 Enterprise Knowledge Platform.** 온프렘 오픈소스 LLM에서 품질과 지연을 동시에 만족하는 것이 운영 조건이다.

---

관련 문서: [`README.md`](README.md) · [`wiki/architecture.md`](wiki/architecture.md) · [`wiki/knowledge-object-schema.md`](wiki/knowledge-object-schema.md) · [`wiki/roadmap.md`](wiki/roadmap.md) · [`wiki/improvement-plan-2026-09-15.md`](wiki/improvement-plan-2026-09-15.md)
