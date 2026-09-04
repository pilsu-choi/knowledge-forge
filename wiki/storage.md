# 저장(Storage)

기본적으로 `knowledge-forge extract`는 Knowledge Object를 stdout/파일로 출력만 한다.
`--store`(기본값 켜짐) + `KF_DATABASE_URL` 설정 시, 파이프라인이 만드는 모든 산출물을
디스크(원본 바이트) + PostgreSQL(구조화 레코드)에 함께 저장한다.

## 왜 원본은 디스크, 나머지는 Postgres인가

- **원본(raw bytes)**: `Source`/`Document`/`Chunk`/`KnowledgeObject`와 달리 검색/조인이 필요
  없는 불투명한 blob이다. 파일시스템이 이미 이 용도에 최적이고, DB에 큰 바이너리를 넣는 것보다
  훨씬 가볍다.
- **구조화 레코드**: id 기반 조회, FK로 provenance 체인 복원, JSONB 필드 조회(예: 특정
  `document_type`의 Knowledge Object 찾기) 같은 관계형 질의가 필요하다 — PostgreSQL이 적합.
- 로컬 파일 소스는 원본이 이미 로컬 디스크에 있지만, 향후 SaaS 커넥터(Slack 메시지, Gmail
  스레드 등)는 원본이 라이브 소스에만 존재한다. 수집 시점에 디스크로 스냅샷을 떠 두면, 원본이
  나중에 수정/삭제되어도 그 시점의 원본 그대로가 남는다. `RawBlobStore`는 이 스냅샷 저장을
  Collector 종류와 무관하게 통일적으로 처리한다.

## 컴포넌트 (`src/knowledge_forge/storage/`)

### `RawBlobStore` (`raw_store.py`)

`RawBlobStore(data_dir).save(item: RawItem, project_id: str = "default") -> Path`.
`content_hash`로 콘텐츠 주소화된 경로(`<data_dir>/raw/<project_id>/<hash[:2]>/<hash>`)에
저장한다 — 같은 프로젝트 안에서 같은 내용의 파일이 여러 번 수집돼도 한 번만 저장된다(dedup은
`project_id` 단위로 스코프된다). `<data_dir>`는 `KF_DATA_DIR`(기본 `./data`).

### `PostgresStore` (`postgres_store.py`)

`psycopg`(v3) 기반. `init_schema()`가 `schema.sql`을 실행한다(모두 `CREATE TABLE/INDEX IF
NOT EXISTS` — 여러 번 실행해도 안전). `save_source`/`save_document`/`save_chunks`/
`save_knowledge_objects`/`save_relations` 다섯 메서드가 각각 `INSERT ... ON CONFLICT (id) DO
UPDATE`로 upsert한다(`save_knowledge_objects`/`save_relations`는 리스트를 받아
`executemany`로 배치 upsert한다 — 한 청크가 여러 `KnowledgeObject`/`Relation`을 낳으므로).

읽기 쪽은 `list_sources`/`get_source`/`list_documents`/`get_document`(둘 다 `project_id`로
필터 가능, `list_documents`는 `source_id`로도 필터 가능) + 기존 `list_knowledge_objects`/
`get_knowledge_object`/`list_relations` — 읽기 API(`api/routes/`)는 이 `PostgresStore`를
직접 의존한다(`PipelineStore`는 쓰기 경로 전용 파사드, `api/deps.py`의 `get_read_store` 참고).

**멱등성(idempotency)**: 모든 테이블의 기본키는 각 모델의 결정론적 `stable_id`
(`src/knowledge_forge/ids.py`)다. 즉 같은 입력으로 파이프라인을 다시 돌리면 같은 id가
나오고, upsert라서 행이 중복 생성되지 않는다 — 재처리/재시도가 항상 안전하다. 실제 임시
Postgres 컨테이너(`docker run postgres:16-alpine`)로 스키마 생성·저장·재실행 idempotency를
라이브로 검증했다(단위 테스트뿐 아니라).

### 스키마 (`schema.sql`)

| 테이블 | 기본키 | 주요 컬럼 | FK |
|---|---|---|---|
| `sources` | `source_id` | `source_type`, `uri`, `content_hash`, `raw_path`(디스크 경로), `metadata`(JSONB), `project_id`(테넌시 키, 기본 `'default'`) | - |
| `documents` | `document_id` | `elements`(JSONB, 요소 전체 직렬화), `metadata`(JSONB), `project_id`(연결된 `sources.project_id`에서 파생) | → `sources` |
| `chunks` | `chunk_id` | `hierarchy_level`, `sequence_index`, `text`, `token_count`, `element_ids`(JSONB), `page_start`/`page_end` | → `documents`, `sources` |
| `knowledge_objects` | `knowledge_object_id` | `type`, `evidence`(JSONB, 이 객체를 뒷받침하는 청크 목록), `summary`, `content`(JSONB, 타입별 payload), `topics`(JSONB), `document_type`, `model_used`, `version`/`pipeline_version`/`source_hash`/`supersedes`(재처리 이력) | → `sources`, `documents` |
| `relations` | `relation_id` | `type`, `subject_id`/`object_id`(둘 다 `knowledge_object_id`를 가리키는 typed edge), `label`, `confidence`, `evidence`(JSONB, chunk_id 목록) | → `knowledge_objects`(양쪽) |
| `document_links` | `link_id`(`stable_id("document_link", type, from, to)`) | `from_document_id`/`to_document_id`(문서 간 typed edge), `link_type`, `confidence`, `metadata`(JSONB) — 아래 "문서 버전 메타데이터와 document_links" 절 | → `documents`(양쪽, CASCADE) |
| `knowledge_object_embeddings` | `embedding_id`(`stable_id(target_kind, 대상 id, embedding_model)`) | `target_kind`(`knowledge_object`/`chunk`), `knowledge_object_id`/`chunk_id`(grain에 따라 하나만), `document_id`(항상), `embedding`(`vector(1536)`, HNSW/cosine 인덱스), `embedded_text`, `embedded_text_hash`, `project_id` | → `knowledge_objects` 또는 `chunks`, `documents` |

`knowledge_objects`는 예전처럼 `chunk_id` 단일 FK를 갖지 않는다 — 하나의 `KnowledgeObject`가
여러 청크에 걸쳐 근거를 가질 수 있어(`evidence`가 리스트) 단일 FK로는 표현이 안 되기
때문이다(`wiki/architecture.md`의 "Chunk와 KnowledgeObject는 1:1이 아니다" 참고). 대신 관계형
질의가 필요하면 `evidence @> '[{"chunk_id": "..."}]'` 같은 JSONB 연산자를 쓴다.

중첩 구조(리스트, 딕셔너리)는 전부 JSONB로 저장한다 — psycopg3가 JSONB ↔ Python
dict/list를 자동 변환해줘서 애플리케이션 코드에서 별도 직렬화가 필요 없다.

**`knowledge_object_embeddings`**: `KnowledgeObject` 또는 원시 `Chunk`(`KF_EMBEDDING_SOURCE`)를
임베딩한 벡터를 담는 별도 테이블 — `target_kind`로 grain을 구분하고 두 grain이 공유하는
`document_id`를 항상 채운다. `knowledge_objects`/`chunks`에 컬럼으로 얹지 않고 분리한 이유,
surrogate PK + partial unique index를 쓰는 이유, HNSW/cosine 인덱스를 고른 이유, 임베딩을
생성/백필하는 방법은 [`embeddings.md`](embeddings.md)에 있다. `docker-compose.yml`의 `postgres` 서비스 이미지는
`pgvector/pgvector:pg16`으로 pgvector 확장이 포함되어 있어야 한다 — 일반 `postgres:16` 이미지로는
`CREATE EXTENSION vector`가 실패한다.

**`project_id`(멀티테넌시 키)**: `sources`/`documents`는 각각 `project_id TEXT NOT NULL
DEFAULT 'default'` 컬럼을 갖는다. `Source`가 만들어질 때(`pipeline._make_source`) 한 번
지정되면 `SourceRef.project_id`를 통해 `Document`까지 그대로 전파되므로, `Document` 모델
자체에는 별도 `project_id` 필드가 없다 — `document.source.project_id`가 곧 그 값이고,
`save_document`는 이를 읽어 `documents.project_id`로 저장한다. 읽기 API(`GET /sources`,
`GET /documents`)는 이 컬럼으로 프로젝트별 목록을 필터링한다 — 자세한 사용법과 접근 제어
관련 한계는 [`api.md`](api.md)의 "멀티테넌시" 절 참고.

### `PipelineStore` (`store.py`)

`RawBlobStore` + `PostgresStore`를 하나로 묶은 파사드. `pipeline.py`는 이 객체 하나만 알면
된다: `record_source(source, item)`(내부적으로 raw 저장 후 경로를 Postgres에 기록),
`record_document`, `record_chunks`, `record_knowledge_objects(objects)`,
`record_relations(relations)`. 컨텍스트 매니저라 `with PipelineStore(...) as store:`로
커넥션 수명을 관리한다.

## 켜고 끄기

- `KF_DATABASE_URL` 환경변수가 설정되어 있어야 저장이 실제로 켜진다. 없으면 `extract`/
  `schedule` 모두 경고를 출력하고 저장 없이(출력만) 진행한다 — 강제 요구사항이 아니라 opt-in.
- `--store`/`--no-store` CLI 플래그로도 껐다 켤 수 있다(기본값 `--store`, 즉 `KF_DATABASE_URL`이
  있으면 기본적으로 저장한다).
- `knowledge-forge db init`으로 스키마를 미리 만들어둘 수 있고, `extract`/`schedule`도 저장이
  켜져 있으면 매 실행 시 `init_schema()`를 자동 호출한다(idempotent라 매번 불러도 무해함).

## 로컬에서 시도해보기

**옵션 A — `uv run`으로 앱은 호스트에서, DB만 컨테이너로:**

```bash
docker compose up -d postgres   # 호스트에 15432 포트로 노출됨 (docker-compose.yml 참고)

export KF_DATABASE_URL="postgresql://kf:kf@localhost:15432/knowledge_forge"
export KF_DATA_DIR="./data"

uv run knowledge-forge db init
uv run knowledge-forge extract tests/fixtures/sample.md --store
```

**옵션 B — 앱까지 컨테이너로 (`docker-compose.yml`의 `app` 서비스가 `KF_DATABASE_URL`을
`postgres:5432`로 이미 설정해둠):**

```bash
export ANTHROPIC_API_KEY=sk-ant-...
docker compose up -d postgres
docker compose run --rm app db init
docker compose run --rm app extract /app/input/sample.md --pretty
```

수집할 파일은 `./input`에 두면 컨테이너 안 `/app/input`으로 읽기 전용 마운트된다. 자세한
컨테이너화 내용은 [`dev-guide.md`](dev-guide.md) 참고.

`psql`로 직접 확인(옵션 A 기준, 호스트 포트 15432):

```bash
psql "postgresql://kf:kf@localhost:15432/knowledge_forge" -c \
  "SELECT knowledge_object_id, summary, document_type FROM knowledge_objects;"
```

## 테스트

`tests/storage/test_raw_store.py`는 순수 디스크 I/O라 항상 실행된다. `tests/storage/
test_postgres_store.py`는 `KF_TEST_DATABASE_URL` 환경변수가 없으면 자동 skip되므로
`uv run pytest`는 Postgres 없이도 항상 통과한다. 실제 DB로 돌리려면:

```bash
docker compose up -d postgres
KF_TEST_DATABASE_URL="postgresql://kf:kf@localhost:15432/knowledge_forge" uv run pytest tests/storage/ -v
```

## 문서 버전 메타데이터와 `document_links`

검색 유형 중 **Version**("최종본/3차 수정본 보여줘")과 **Parent/Child**("이 스레드 앞 대화",
"이전 버전")는 본문만으로는 못 푼다 — 두 개정본은 문장 대부분이 같고, 신호는 사람이 붙인 파일명
(`_v2`, `(1)`, `3차 수정본`, `최종`, `_260824`)과 소스 메타데이터에 있다. 그래서 인제스트 시점에
한 번 뽑아 둔다(`src/knowledge_forge/ingest_version.py` + `document_links.py`, 배선은
`pipeline.run_pipeline`의 `apply_version_metadata` → `record_document` → `link_document`).

### `documents.metadata`의 version 키

`DocumentMetadata`에 `version_label`/`version_number`/`is_final`/`version_family`가 추가됐다
(JSONB에 그대로 들어가므로 컬럼 추가 없음). `version_family` expression index
(`idx_documents_version_family`)만 얹어 "같은 family 다른 버전" 조회를 빠르게 한다.

| 키 | 예 | 설명 |
|---|---|---|
| `version_family` | `equipment cata` | 버전 표기·확장자를 걷어내고 NFKC + casefold + 구분자 통일한 이름. 같은 문서의 여러 버전을 묶는 키 |
| `version_label` | `v2.1`, `3차 수정본`, `v3_최종`, `(1)`, `20260824` | 표기 원문. 날짜 접미만 있으면 `YYYYMMDD`로 정규화 |
| `version_number` | `2.1`, `3.0`, `1.0`, `0.0`(초안/원본) | 숫자 표기. `최종`/`수정본`/날짜만 있으면 NULL |
| `is_final` | `true` | 최종/final/확정/latest 표기 |

**왜 규칙 기반인가**: 어휘가 작고 안정적이며(`v`/`ver`/`rev`/`r`/`N차`/`(N)`/최종/초안/수정/날짜),
문서마다 LLM 호출을 하면 파서 단계 전체보다 비싸고, 무엇보다 `family_key`는 **결정론적**이어야
몇 달 뒤 들어온 파일이 같은 family에 떨어진다. 규칙은 개발 DB의 Google Drive 파일명 246개에
대고 맞췄다 — 이 중 명시적 표기가 있는 건 24개(v: 9, R/r: 3, (1): 3, 날짜 접미: 3, 최종/final:
2, 초안: 1 …), 2건 이상 family 중 서로 다른 버전이 실제로 있는 건 `Equipment_Cata_ver12.0 /
ver13.0` 하나뿐이고 나머지 다건 family는 같은 이름 재업로드(`image.png` ×65, `routing_sample.xlsx`
×2)였다.

데이터에서 나온 오탐 때문에 생긴 규칙들:
- **`N차`는 수식어(수정본/개정/버전/본)가 붙거나 이름 끝에 있을 때만** 버전이다 —
  "3차 인터뷰를 위한 회의자료"의 `3차`는 서수다.
- **날짜는 (다른 표기를 지운 뒤) 이름 끝에 있을 때만** 버전 스탬프다 — `IMG_20260824_230138`,
  `스크린샷 2026-07-31 오후 5.57.35`는 촬영 시각이지 개정이 아니다. 끝에 없는 날짜는 family 키에
  그대로 남겨 스크린샷 수십 장이 한 family로 묶이지 않게 한다.
- **`(N)` 복사본 표기는 1–2자리 + 이름 끝**만 — `CAPA CHART(2021)`의 `(2021)`은 연도다.
- 표기가 여러 개면 **이름 끝에 가장 가까운 것**이 label/number가 되고(`Simulation_V2 패치
  검토_R1` → R1), family 키에서는 **전부** 제거한다(V2/V3 패치 검토가 같은 family가 되도록).
- `v260831`처럼 v 뒤가 날짜 모양이면 번호가 아니라 날짜로 본다.
- 정렬(`ingest_version.version_sort_key`, `list_version_family`가 사용): 초안/원본(0) <
  표기 없음(1) < 번호/날짜(2, 번호→날짜 순) < 번호 없는 수정본(3), 그리고 `is_final`은 항상 마지막.
  SQL `ORDER BY`가 아니라 Python 정렬인 이유는 이 순위가 세 키의 함수이기 때문.

### `document_links` 테이블

| 컬럼 | 설명 |
|---|---|
| `link_id` | `stable_id("document_link", link_type, from, to)` — 재실행 시 upsert |
| `from_document_id` / `to_document_id` | 둘 다 `documents` FK, `ON DELETE CASCADE` |
| `link_type` | `models/link.py::LinkType` 값 |
| `confidence`, `metadata`(JSONB), `created_at` | |

`relations`(KnowledgeObject 간)와 별개이고, OKF `links/*.yaml`((source_id, document_id) 쌍 키)의
Postgres 대응물이다 — `document_id`만으로 키를 잡아 `search/sql.py`가 `documents`에 바로 JOIN할 수
있게 했다. 방향은 타입 이름대로 읽는다(`from`이 `to`의 PREVIOUS_VERSION을 가짐 / `from`이 `to`의
ATTACHMENT_OF / `from`의 THREAD_PARENT가 `to`). 시퀀스 타입 둘은 `pipeline._resolve_links`의
기존 방향(앞선 것 → 다음 것)을 유지한다.

**지금 만드는 타입**
- `previous_version` — 같은 `project_id` + 같은 `version_family` 안에서, 정렬 순으로 인접한
  두 문서 중 **버전 키가 다른** 쌍마다 `최신 → 직전`. family 단위로 기존 체인을 지우고 다시 만들기
  때문에(`relink_version_family`) v3 뒤에 v2가 늦게 들어와도 v3→v2, v2→v1로 재배선된다. 버전 키가
  같은 쌍(`image.png` 두 번, "주간보고 v1"과 "Re: 주간보고 v1")은 같은 버전의 사본이지 개정이
  아니라서 잇지 않는다 — 안 그러면 `image.png` ×65가 64개의 가짜 "이전 버전"이 된다. 표기 없는
  원본 → `_v2`는 잇는다(첫 버전은 보통 그렇게 이름 붙인다).
- `same_channel_sequence` / `thread_sequence` — `sources.metadata.links`를 스토어에서 해석
  ([`context-reconstruction.md`](context-reconstruction.md) 마지막 절).
- `thread_parent` — Slack 스레드 에피소드 → 부모 메시지를 담은 타임라인 에피소드(증분 동기화로
  둘이 따로 적재된 경우에만 존재).

**enum에만 있고 아직 안 만드는 타입** — `attachment_of`(Slack/Gmail 커넥터가 첨부파일을 아직
수집하지 않음, [`saas-connectors.md`](saas-connectors.md)), `parent_folder`(GDrive 커넥터가
`parents`를 가져오지 않고, 폴더 자체가 Document가 아니라 연결할 끝점이 없음). 커넥터가 그 정보를
채우는 날 `build_source_links`에 분기 하나를 더하면 된다.

### 백필

`knowledge-forge db backfill-links [--project-id X]` →
`PipelineStore.backfill_document_versions_and_links`. 모든 문서를 훑어 version 키를
`metadata || {...}`로 **병합**(다른 키 — title 등 — 은 보존), family마다 previous_version 체인을
재구성, 소스 메타데이터 링크를 upsert. 멱등이라 몇 번 돌려도 같다. 개발 DB(문서 263건) 실행
결과: family 부여 66건(gmail 59 + gdrive 7; Slack 197건은 설계상 제외), 명시적 표기 0건(문서가
있는 Drive 파일 7개가 전부 handover 문서라 표기가 없음), `same_channel_sequence` 145건,
`thread_sequence` 58건, `previous_version`/`thread_parent` 0건.

### `search/sql.py`에서의 사용

`SCHEMA_DESCRIPTION`에 version 키와 `document_links`를 설명해 두었다 — "최종본/최신 버전"
질문은 이름으로 후보를 찾고 그 `version_family`로 묶은 뒤 `is_final` 우선, 없으면
`version_number DESC`, 그래도 없으면 `created_at DESC`로 고르라는 안내.
