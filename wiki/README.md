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
| [`context-reconstruction.md`](context-reconstruction.md) | Slack 대화 세그멘테이션·Gmail 스레드 병합 — RawItem 하나가 항상 하나의 의미 단위는 아니라는 문제와 해법 |

## 파싱 · 청킹 · 추출

| 문서 | 내용 |
|---|---|
| [`parsers.md`](parsers.md) | 포맷별(PDF/DOCX/XLSX/PPTX/HWPX/Image/Markdown/TXT) 지원 현황, 라이브러리, 라이선스 근거 |
| [`vision-extraction.md`](vision-extraction.md) | 표/이미지/차트를 VLM으로 처리하는 하이브리드 파싱 설계 — 왜 "항상 VLM"인지, bbox 중복 제거, 실패 시 성능 저하 |
| [`adaptive-chunking.md`](adaptive-chunking.md) | Document→Chapter→Section→...→Line 구조 기반 청킹 알고리즘, "최대 semantic unit 유지" 원칙 |
| [`llm-providers.md`](llm-providers.md) | Knowledge Extraction/Vision의 LLM Provider 추상화 — Anthropic(기본) ↔ OpenRouter(대체) 전환 |

## 저장 · 운영

| 문서 | 내용 |
|---|---|
| [`storage.md`](storage.md) | `PipelineStore`(디스크 원본 + PostgreSQL 구조화 레코드) 스키마와 opt-in 동작 |
| [`okf.md`](okf.md) | `OKFWriter` — manifest.yaml + resources/objects/relations 파일 트리로 된 Knowledge Base, Postgres와 독립적인 opt-in 출력 |
| [`scheduling.md`](scheduling.md) | `knowledge-forge schedule`의 tick/interval 모델, 실패 격리 |
| [`dev-guide.md`](dev-guide.md) | 셋업(`uv sync`), 환경변수 전체 목록, CLI 사용법, 컨테이너 실행, 새 Parser/Collector 추가 방법 |
