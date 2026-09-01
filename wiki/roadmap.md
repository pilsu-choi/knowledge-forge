# 로드맵

## 완료된 범위 (Phase 1 + Phase 2 + Phase 3)

- **전체 아키텍처 스캐폴딩**: Collector/Parser/Adaptive Chunker/Knowledge Extraction/Knowledge
  Object 전 구간의 인터페이스와 모델을 PRD가 명시한 4개 SaaS + 8개 파일 포맷을 아우르도록 설계.
- **모든 파일 포맷 end-to-end 동작**: PDF/DOCX/Markdown/TXT/XLSX/PPTX/Image/**HWPX** → Parser →
  Adaptive Chunker → Claude 기반 Knowledge Extraction → Knowledge Object. PDF/Image/PPTX/HWPX/
  DOCX의 이미지, PDF의 표는 VLM 하이브리드로, PPTX 네이티브 차트는 결정론적 데이터 추출로
  처리한다([`vision-extraction.md`](vision-extraction.md), [`parsers.md`](parsers.md)).
- **4개 SaaS 커넥터 전부 실구현**: Slack/Gmail/Google Drive/Notion — 인증된 클라이언트를
  주입받아 초기/증분 동기화 수행([`saas-connectors.md`](saas-connectors.md)). 모킹 테스트만
  검증됨, CLI 연동은 아직 없음(아래 참고).
- **저장(Storage)**: 원본은 디스크(content-addressed), Source/Document/Chunk/KnowledgeObject는
  PostgreSQL — opt-in, `--store`/`KF_DATABASE_URL`([`storage.md`](storage.md)).
- **스케줄링**: `knowledge-forge schedule`로 프로세스 내 주기 실행
  ([`scheduling.md`](scheduling.md)).
- **컨테이너화**: `Dockerfile` + `docker-compose.yml`(앱 + Postgres) — [`dev-guide.md`](dev-guide.md).
- **인증**: Anthropic 자격 증명(`ANTHROPIC_API_KEY` 또는 `ant auth login` OAuth) 사전 점검 +
  친절한 에러 메시지.

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

- **Batches API**: 현재는 청크당(그리고 표/이미지 하나당) 1회 동기 LLM 호출이라, 대량
  문서·표·이미지가 많은 문서 처리 시 비용/지연이 커진다. Anthropic Message Batches
  API(비동기, 표준 대비 50% 비용)로 전환하면 대량 배치 처리에 유리하다 — Knowledge
  Extraction과 Vision 추출 양쪽 다 후보.
- 토큰 카운팅 정확도 개선(`messages.count_tokens`로 청크 경계 최종 검증).
- 인덱싱/저장 레이어(위 "제외 범위" 표 참고) 연동은 이 파이프라인의 출력(JSONL 또는
  PostgreSQL의 `knowledge_objects` 테이블)을 그대로 소비하는 형태로 설계되어 있어 별도
  어댑터만 추가하면 된다.
- ~~저장(Storage)~~ — 완료([`storage.md`](storage.md)).
- ~~스케줄링~~ — 완료([`scheduling.md`](scheduling.md)).

### 4단계 — Source-aware Context Reconstruction (설계 방향, 미구현)

사용자가 제시한 설계 방향을 액션 아이템으로 기록한다. **아직 구현되지 않음** — 1단계(SaaS
CLI/인증 연동)로 실제 SaaS 소스를 붙이기 시작할 때 반드시 반영해야 할 아키텍처 변경이다.

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

1. **Source ≠ Knowledge Unit** — 각 Collector가 만든 `RawItem`을 곧바로 Chunk로 넘기지 않는다.
   소스별로 "Context Reconstruction" 단계를 Parser와 Adaptive Chunker 사이(또는 Parser 내부의
   전처리 단계)에 새로 둔다.
2. **소스별 Context Reconstruction 전략**:
   - File(PDF/DOCX/XLSX/PPTX/Image/HWPX): 파일 자체가 독립 정보 단위 — 지금처럼 Parsing →
     바로 Chunking (변경 없음).
   - Notion: Page → Block/Section 구조가 이미 명확 — 지금처럼 즉시 Parsing (변경 없음).
   - GDrive(Google Docs 등): Document → 구조 Parsing — 지금처럼 즉시 Parsing (변경 없음).
   - **Gmail**: 개별 이메일이 아니라 **Thread**를 semantic unit으로 재구성하는 단계 필요
     (`threadId`로 그룹핑 후 시간순 정렬해 하나의 대화로 합치는 로직 — `GmailCollector`가
     이미 `metadata`에 `thread_id`를 담고 있어 나중에 이 재구성 단계를 추가하기 쉬움).
   - **Slack**: 메시지를 곧바로 청크하지 않고 **Conversation Reconstruction →
     Conversation Segmentation**(대화 흐름이 끊기는 지점을 찾아 에피소드 단위로 분할) 단계가
     필요. `SlackCollector`가 이미 스레드(`thread_ts`)는 별도로 수집하지만, 스레드가 아닌
     연속 메시지들 간의 대화 경계 탐지는 아직 없음 — 신규 구현 필요.
3. **첨부파일은 항상 File Pipeline으로 라우팅** — Slack/Gmail/Notion/GDrive 어디서 오든
   첨부파일이 발견되면 기존 File Collector→Parser 경로로 넘긴다(별도 재구현 없이 기존
   파이프라인 재사용). 현재 4개 SaaS 커넥터는 아직 첨부파일 감지/추출 자체를 하지 않음
   ([`saas-connectors.md`](saas-connectors.md)) — 이것도 1단계에서 함께 다뤄야 한다.
4. **Ingest와 Process 분리** — 사용자가 소스를 연결/업로드하면 "수집 완료"를 즉시 응답하고,
   무거운 Parsing/Chunking/Knowledge Extraction은 백그라운드 워커로 넘긴다. 현재
   `knowledge-forge extract`는 동기 실행이라 이 분리가 없다 — SaaS 커넥터가 CLI에 붙는
   시점(1단계)에 큐/워커 아키텍처(또는 최소한 `schedule` 명령의 비동기화)를 함께 설계해야
   한다.
5. **스케줄링 성격 구분** — Slack/Gmail처럼 계속 생성되는 데이터는 polling(현재
   `knowledge-forge schedule`이 이 모델) 또는 향후 webhook/event-driven ingest가 맞고, 정적
   파일 소스는 최초 1회 + 변경 감지 재처리로 충분하다는 게 이미 맞게 설계되어 있음 — 다만
   Slack/Gmail의 짧은 polling 주기가 필요해질 경우 API rate limit 대응(3단계에서 이미 언급)이
   더 중요해진다.

**한 줄 정의(향후 아키텍처 문서화 시 재사용)**: "Multi-source SaaS 및 File 데이터를 수집하고,
source별 semantic context를 복원한 뒤 adaptive chunking과 knowledge extraction을 수행하여
Knowledge Base를 구축하는 ingestion pipeline."
