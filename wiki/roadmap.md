# 로드맵

## MVP 범위

- **전체 아키텍처 스캐폴딩**: Collector/Parser/Adaptive Chunker/Knowledge Extraction/Knowledge
  Object 전 구간의 인터페이스와 모델을 PRD가 명시한 4개 SaaS + 8개 파일 포맷을 아우르도록 설계.
- **실제 동작하는 수직 슬라이스**: 로컬 파일(PDF/DOCX/Markdown/TXT) → Parser → Adaptive Chunker
  → Claude 기반 Knowledge Extraction → Knowledge Object까지 end-to-end로 동작.
- SaaS 커넥터(Slack/Gmail/GoogleDrive/Notion)와 나머지 파일 포맷(XLSX/PPTX/Image/HWPX)은
  인터페이스만 정의.

## 제외 범위와 "왜 막지 않는가"

PRD 5장이 명시한 제외 범위는 Vector DB / Graph DB / Ontology-OWL / Search-RAG / Agent /
Dashboard / Chat UI다. 이들을 지금 만들지 않는 이유는 스코프 관리이지, 설계가 이들을 나중에
막기 때문이 아니다.

| 제외 항목 | 현재 설계가 막지 않는 이유 |
|---|---|
| Vector DB | `KnowledgeObject.summary`/`facts`/`entities`/`topics`는 이미 임베딩하기 좋은 평탄한 텍스트 필드로 분리되어 있다. 벡터 스토어에 넣을 때 `KnowledgeObject`를 그대로 소스 레코드로 사용하면 된다. |
| Graph DB | `entities`/`relations`는 이미 subject-predicate-object 형태로 추출되어 있어 그래프의 노드/엣지로 직접 매핑 가능하다. `SourceRef`/`DocumentRef`/`ChunkRef`의 id 체인은 그래프가 필요로 하는 "출처로 돌아가는 엣지" 그 자체다. |
| Ontology/OWL | `Entity.type`/`document_type`이 자유 문자열이라, 추후 온톨로지 클래스 체계로 매핑하는 정규화 레이어를 얹기만 하면 된다. 지금 스키마를 바꿀 필요가 없다. |
| Search/RAG | Knowledge Object 자체가 검색 인덱싱의 입력 단위로 설계되어 있다(청크 단위 + 요약 + 메타데이터). RAG 파이프라인은 이 위에 검색/재순위 레이어만 추가하면 된다. |
| Agent | Knowledge Extraction은 순수 함수형 파이프라인 단계(청크 → LLM 호출 → 구조화 출력)로, 에이전트형 도구 호출 루프가 필요 없다. 나중에 에이전트가 필요해지면 이 파이프라인을 도구(tool)로 감싸기만 하면 된다. |
| Dashboard / Chat UI | 순수 데이터 처리 파이프라인이라 UI 레이어와 결합되어 있지 않다. CLI/JSONL 출력이 어떤 UI 위에도 얹힐 수 있는 형태다. |

## 단계별 후속 계획

### 1단계 — 실제 SaaS 커넥터 구현

- Slack: `slack_sdk.WebClient.conversations_history`/`conversations_replies` 기반, 채널/스레드
  단위 초기·증분 동기화.
- Gmail: `google-api-python-client`의 `users().messages().list/get`, `query` 기반 필터링.
- Google Drive: `files().list`/`files().get_media`/`files().export_media`, `modifiedTime` 기반
  증분 동기화.
- Notion: `notion_client.Client.search`/`blocks.children.list` 재귀 순회, `last_edited_time`
  기반 증분 동기화.
- 공통으로 인증 정보 관리(OAuth 토큰 저장/갱신), rate limit 대응이 필요.

### 2단계 — 나머지 파일 포맷

- XLSX(`openpyxl`), PPTX(`python-pptx`)는 라이브러리가 이미 성숙해 상대적으로 빠르게 구현 가능.
- Image OCR(`Pillow`+`pytesseract`)은 시스템 의존성(tesseract-ocr, 언어팩) 설치 및 배포 전략
  필요.
- HWPX는 별도 스코프의 커스텀 파서 개발이 필요 — [`parsers.md`](parsers.md) 참고.

### 3단계 — 운영 최적화

- **Batches API**: 현재는 청크당 1회 동기 LLM 호출이라, 대량 문서 처리 시 비용/지연이 커진다.
  Anthropic Message Batches API(비동기, 표준 대비 50% 비용)로 전환하면 대량 배치 처리에 유리하다.
- 토큰 카운팅 정확도 개선(`messages.count_tokens`로 청크 경계 최종 검증).
- 인덱싱/저장 레이어(위 "제외 범위" 표 참고) 연동은 이 파이프라인의 출력(JSONL)을 그대로
  소비하는 형태로 설계되어 있어 별도 어댑터만 추가하면 된다.
