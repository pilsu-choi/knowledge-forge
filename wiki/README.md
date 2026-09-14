# Knowledge Forge Wiki 인덱스

전체 그림을 먼저 보려면 [`architecture.md`](architecture.md)와 [`roadmap.md`](roadmap.md)부터.

## 핵심 개념

| 문서 | 내용 |
|---|---|
| [`architecture.md`](architecture.md) | 파이프라인 전체 구조(Collector→Context Reconstruction→Parser→Chunker→Extraction), 컴포넌트별 책임, 확장 포인트, provenance/타입드 오브젝트/hierarchy 설계 결정 |
| [`knowledge-object-schema.md`](knowledge-object-schema.md) | `KnowledgeObject`/`Relation`/`Source`/`Document`/`Chunk` 필드별 스펙과 예시 JSON |
| [`roadmap.md`](roadmap.md) | 완료된 범위, PRD 제외 범위가 왜 설계를 막지 않는지, 단계별 후속 계획 |

## 수집 (Collector · Context Reconstruction)

| 문서 | 내용 |
|---|---|
| [`saas-connectors.md`](saas-connectors.md) | Slack/Gmail/GoogleDrive/Notion 커넥터 — 인증 클라이언트 주입 설계, 초기/증분 동기화, 삭제 감지 방식 |
| [`reingest-replay.md`](reingest-replay.md) | `knowledge-forge reparse` — 이미 디스크에 있는 원본 바이트를 재수집 없이 다시 파싱하기. GDrive 239건 미적재 사건의 원인·복구 결과·남은 후속 과제 |
| [`context-reconstruction.md`](context-reconstruction.md) | Slack 대화 세그멘테이션·Gmail 스레드 병합 — RawItem 하나가 항상 하나의 의미 단위는 아니라는 문제와 해법 |

## 파싱 · 청킹 · 추출

| 문서 | 내용 |
|---|---|
| [`parsers.md`](parsers.md) | 포맷별(PDF/DOCX/XLSX/PPTX/HWPX/HTML/Image/Markdown/TXT) 지원 현황, 라이브러리, 라이선스 근거 |
| [`vision-extraction.md`](vision-extraction.md) | 표/이미지/차트를 VLM으로 처리하는 하이브리드 파싱 설계 — 왜 "항상 VLM"인지, bbox 중복 제거, 실패 시 성능 저하 |
| [`adaptive-chunking.md`](adaptive-chunking.md) | Document→Chapter→Section→...→Line 구조 기반 청킹 알고리즘, "최대 semantic unit 유지" 원칙 |
| [`llm-providers.md`](llm-providers.md) | Knowledge Extraction/Vision의 LLM Provider 추상화 — Anthropic(기본) ↔ OpenRouter(대체) 전환 |

## 저장 · 운영

| 문서 | 내용 |
|---|---|
| [`storage.md`](storage.md) | `PipelineStore`(디스크 원본 + PostgreSQL 구조화 레코드) 스키마와 opt-in 동작 |
| [`embeddings.md`](embeddings.md) | `KnowledgeObject` 또는 원시 `Chunk`를 pgvector에 임베딩하는 파이프라인 — `EmbeddingProvider` 추상화, `KF_EMBEDDING_SOURCE`(청크 기반 vs KO 기반, `--no-extract`), `knowledge_object_embeddings` 스키마(`target_kind`/`document_id`), `--embed`/`knowledge-forge embed`, staleness 미자동화 |
| [`agent-search.md`](agent-search.md) | 에이전트 검색 설계 — pgvector/임베딩과 `vector_search`/`plan_search`/`sql_search`(Query Planning+Routing+Text-to-SQL) agent 도구(구현됨), `POST /search/vector` HTTP 엔드포인트/`depth>1` 그래프 확장/MCP 서버/검색 skill(설계만, 미구현) |
| [`agent-runtime.md`](agent-runtime.md) | 모델 벤더 중립 Agent Runtime(harness) — `file_search`/`vector_search`/`sql_search`/`plan_search`/FileSystem/Memory/SubAgents 도구를 실제로 쥐고 도는 `knowledge-forge agent` CLI(구현됨), 자체 `ChatProvider` 추상화(Anthropic 네이티브 + OpenAI-호환) |
| [`search-architecture.md`](search-architecture.md) | 넓은 엔터프라이즈 RAG 아키텍처 레이어별 상태 — Query Planning/Routing/Reranking(RRF+cross-source dedup)/Temporal Search/Text-to-SQL(entity 필터+temporal join+graph 1-hop 대체)는 구현됨, Context Engineering 나머지/Map-Reduce/Evidence Grounding/Feedback Loop는 설계만·미구현, Iterative Retrieval은 이미 tool-calling 루프로 충족 |
| [`evaluation.md`](evaluation.md) | 검색 평가(2026-09-14 재구현) — grain(chunk/KO) 고정 골든셋 자동 생성(`eval gen`)과 자동 게이트, retrieval 지표(기본)·에이전트 답변 채점(`--e2e`), paired bootstrap 비교(`eval compare`) |
| [`query-types.md`](query-types.md) | 검색 유형 20종(Navigational/Temporal/Metadata/Exact/Aggregation/Negative/Comparison/...)별 "왜 유사도가 어려운가 / 필요한 전략 / `search/constraints.py`의 어떤 플래그가 잡는가 / 현재 상태" 표 |
| [`okf.md`](okf.md) | `OKFWriter` — manifest.yaml + resources/objects/relations 파일 트리로 된 Knowledge Base, Postgres와 독립적인 opt-in 출력 |
| [`ui.md`](ui.md) | `/ui` — React+Vite 기반 OKF 뷰어(마크다운+그래프). Tailwind+shadcn/ui 기반 Obsidian풍 비주얼, 디렉터리 구조, 알려진 CSS/타입 함정 |
| [`scheduling.md`](scheduling.md) | `knowledge-forge schedule`의 tick/interval 모델, 실패 격리 |
| [`api.md`](api.md) | `knowledge-forge serve`(FastAPI) + `worker` — Ingest/Process 분리, Postgres job queue, 엔드포인트 목록 |
| [`dev-guide.md`](dev-guide.md) | 셋업(`uv sync`), 환경변수 전체 목록, CLI 사용법, 컨테이너 실행, 새 Parser/Collector 추가 방법 |
