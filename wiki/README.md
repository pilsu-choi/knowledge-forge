# Knowledge Forge Wiki 인덱스

전체 그림을 먼저 보려면 [`architecture.md`](architecture.md)와 [`roadmap.md`](roadmap.md)부터.

## 핵심 개념

| 문서 | 내용 |
|---|---|
| [`architecture.md`](architecture.md) | 파이프라인 전체 구조(Collector→Context Reconstruction→Parser→Chunker→Extraction), 컴포넌트별 책임, 확장 포인트, provenance/타입드 오브젝트/hierarchy 설계 결정 |
| [`knowledge-object-schema.md`](knowledge-object-schema.md) | `KnowledgeObject`/`Relation`/`Source`/`Document`/`Chunk` 필드별 스펙과 예시 JSON |
| [`roadmap.md`](roadmap.md) | 완료된 범위, PRD 제외 범위가 왜 설계를 막지 않는지, 단계별 후속 계획 |
| [`improvement-plan-2026-09-15.md`](improvement-plan-2026-09-15.md) | 개선 필요 사항 — Foundry형 온톨로지·오픈소스 LLM(Qwen3.8-27B)·품질과 지연 동시 달성 기준의 진단. 측정(e2e 부재), 에이전트 런타임(reasoning·max_tokens), 지연 구조(LLM 호출 수·프리필), 검색 코어(BM25·리랭커·온프렘 임베딩), 온톨로지(엔티티 해소·타입 속성·그래프 읽기·MCP)와 추천 순서 |

## 수집 (Collector · Context Reconstruction)

| 문서 | 내용 |
|---|---|
| [`saas-connectors.md`](saas-connectors.md) | Slack/Gmail/GoogleDrive/Notion 커넥터 — 인증 클라이언트 주입 설계, 초기/증분 동기화, 삭제 감지 방식 |
| [`reingest-replay.md`](reingest-replay.md) | `knowledge-forge reparse` — 이미 디스크에 있는 원본 바이트를 재수집 없이 다시 파싱하기. GDrive 239건 미적재 사건, 재생 소스의 재구성 생략(슬랙 KeyError·Gmail 문서 중복 수정), 임베딩 요청 토큰 예산 분할, 재적재 시 문서 청크 집합 교체, 변경 없는 소스 건너뛰기, 처리량 실측(2026-09-14) |
| [`context-reconstruction.md`](context-reconstruction.md) | Slack 대화 세그멘테이션·Gmail 스레드 병합 — RawItem 하나가 항상 하나의 의미 단위는 아니라는 문제와 해법 |

## 파싱 · 청킹 · 추출

| 문서 | 내용 |
|---|---|
| [`parsers.md`](parsers.md) | 포맷별(PDF/DOCX/XLSX/PPTX/HWPX/HTML/Image/Markdown/TXT) 지원 현황, 라이브러리, 라이선스 근거 |
| [`vision-extraction.md`](vision-extraction.md) | 표/이미지/차트를 VLM으로 처리하는 하이브리드 파싱 설계 — 왜 "항상 VLM"인지, bbox 중복 제거, 실패 시 성능 저하 |
| [`adaptive-chunking.md`](adaptive-chunking.md) | Document→Chapter→Section→...→Line 구조 기반 청킹 알고리즘, "최대 semantic unit 유지" 원칙, 표 행 분할 O(n) 누적, 작은 형제 청크 병합(`KF_MIN_TOKENS_PER_CHUNK`)과 복제 DB A/B 실측 |
| [`llm-providers.md`](llm-providers.md) | Knowledge Extraction/Vision의 LLM Provider 추상화 — Anthropic(기본) ↔ OpenRouter(대체) 전환 |

## 저장 · 운영

| 문서 | 내용 |
|---|---|
| [`storage.md`](storage.md) | `PipelineStore`(디스크 원본 + PostgreSQL 구조화 레코드) 스키마와 opt-in 동작 |
| [`embeddings.md`](embeddings.md) | `KnowledgeObject` 또는 원시 `Chunk`를 pgvector에 임베딩하는 파이프라인 — `EmbeddingProvider` 추상화, `KF_EMBEDDING_SOURCE`(청크 기반 vs KO 기반, `--no-extract`), `knowledge_object_embeddings` 스키마(`target_kind`/`document_id`), `--embed`/`knowledge-forge embed`, staleness 미자동화 |
| [`agent-search.md`](agent-search.md) | 에이전트 검색 설계 — pgvector/임베딩과 `vector_search`/`plan_search`/`sql_search`(Query Planning+Routing+Text-to-SQL) agent 도구(구현됨), `POST /search/vector` HTTP 엔드포인트/`depth>1` 그래프 확장/MCP 서버/검색 skill(설계만, 미구현) |
| [`agent-runtime.md`](agent-runtime.md) | 모델 벤더 중립 Agent Runtime(harness) — `file_search`/`vector_search`/`sql_search`/`plan_search`/FileSystem/Memory/SubAgents 도구를 실제로 쥐고 도는 `knowledge-forge agent` CLI(구현됨), 자체 `ChatProvider` 추상화(Anthropic 네이티브 + OpenAI-호환) |
| [`search-architecture.md`](search-architecture.md) | 넓은 엔터프라이즈 RAG 아키텍처 레이어별 상태 — Query Planning/Routing/Reranking/Temporal Search/Text-to-SQL 구현 현황, Retrieval Plan 실행기 규칙, 어휘 검색 SQL의 trigram 인덱스 활용(2026-09-14), 랭킹 구조 수정 7건 A/B와 채택 결과, 평가 재구현 연결 |
| [`evaluation.md`](evaluation.md) | 검색 평가(2026-09-14 재구현) — grain(chunk/KO) 고정 골든셋 자동 생성(`eval gen`)과 자동 게이트, retrieval 지표(기본)·에이전트 답변 채점(`--e2e`), paired bootstrap 비교(`eval compare`) |
| [`search-code-review-2026-09-14.md`](search-code-review-2026-09-14.md) | 검색 파이프라인·에이전트 경로 코드 리뷰와 `fix/search-parsing` 수정 내역(시간 파싱, 마커 경계, `project_id` 검증, 깨진 JSON), 회귀 probe |
| [`search-quality-review-2026-09-15.md`](search-quality-review-2026-09-15.md) | 검색·평가 품질 조사(미수정) — 질의 해석 오탐이 믿는 필터로 들어가는 문제, 문서 단위 RRF 미합산·anchor 격차 소수점 버그, 평가의 라벨 풀링 편향·none 임계값 결합·dev/test 누수, 추천 순서와 재현 |
| [`query-types.md`](query-types.md) | 검색 유형 20종(Navigational/Temporal/Metadata/Exact/Aggregation/Negative/Comparison/...)별 "왜 유사도가 어려운가 / 필요한 전략 / `search/constraints.py`의 어떤 플래그가 잡는가 / 현재 상태" 표 |
| [`okf.md`](okf.md) | `OKFWriter` — manifest.yaml + resources/objects/relations 파일 트리로 된 Knowledge Base, Postgres와 독립적인 opt-in 출력 |
| [`ui.md`](ui.md) | `/ui` — React+Vite 기반 OKF 뷰어(마크다운+그래프). Tailwind+shadcn/ui 기반 Obsidian풍 비주얼, 디렉터리 구조, 알려진 CSS/타입 함정 |
| [`scheduling.md`](scheduling.md) | `knowledge-forge schedule`의 tick/interval 모델, 실패 격리 |
| [`api.md`](api.md) | `knowledge-forge serve`(FastAPI) + `worker` — Ingest/Process 분리, Postgres job queue, 엔드포인트 목록 |
| [`dev-guide.md`](dev-guide.md) | 셋업(`uv sync`), 환경변수 전체 목록, CLI 사용법, 컨테이너 실행, 새 Parser/Collector 추가 방법 |
