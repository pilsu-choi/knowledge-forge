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

`RawBlobStore(data_dir).save(item: RawItem) -> Path`. `content_hash`로 콘텐츠 주소화된
경로(`<data_dir>/raw/<hash[:2]>/<hash>`)에 저장한다 — 같은 내용의 파일이 여러 번 수집돼도
한 번만 저장된다(dedup). `<data_dir>`는 `KF_DATA_DIR`(기본 `./data`).

### `PostgresStore` (`postgres_store.py`)

`psycopg`(v3) 기반. `init_schema()`가 `schema.sql`을 실행한다(모두 `CREATE TABLE/INDEX IF
NOT EXISTS` — 여러 번 실행해도 안전). `save_source`/`save_document`/`save_chunks`/
`save_knowledge_object` 네 메서드가 각각 `INSERT ... ON CONFLICT (id) DO UPDATE`로
upsert한다.

**멱등성(idempotency)**: 모든 테이블의 기본키는 각 모델의 결정론적 `stable_id`
(`src/knowledge_forge/ids.py`)다. 즉 같은 입력으로 파이프라인을 다시 돌리면 같은 id가
나오고, upsert라서 행이 중복 생성되지 않는다 — 재처리/재시도가 항상 안전하다. 실제 임시
Postgres 컨테이너(`docker run postgres:16-alpine`)로 스키마 생성·저장·재실행 idempotency를
라이브로 검증했다(단위 테스트뿐 아니라).

### 스키마 (`schema.sql`)

| 테이블 | 기본키 | 주요 컬럼 | FK |
|---|---|---|---|
| `sources` | `source_id` | `source_type`, `uri`, `content_hash`, `raw_path`(디스크 경로), `metadata`(JSONB) | - |
| `documents` | `document_id` | `elements`(JSONB, 요소 전체 직렬화), `metadata`(JSONB) | → `sources` |
| `chunks` | `chunk_id` | `hierarchy_level`, `sequence_index`, `text`, `token_count`, `element_ids`(JSONB), `page_start`/`page_end` | → `documents`, `sources` |
| `knowledge_objects` | `knowledge_object_id` | `summary`, `facts`/`entities`/`relations`/`topics`(JSONB), `document_type`, `model_used` | → `sources`, `documents`, `chunks` |

중첩 구조(리스트, 딕셔너리)는 전부 JSONB로 저장한다 — psycopg3가 JSONB ↔ Python
dict/list를 자동 변환해줘서 애플리케이션 코드에서 별도 직렬화가 필요 없다.

### `PipelineStore` (`store.py`)

`RawBlobStore` + `PostgresStore`를 하나로 묶은 파사드. `pipeline.py`는 이 객체 하나만 알면
된다: `record_source(source, item)`(내부적으로 raw 저장 후 경로를 Postgres에 기록),
`record_document`, `record_chunks`, `record_knowledge_object`. 컨텍스트 매니저라 `with
PipelineStore(...) as store:`로 커넥션 수명을 관리한다.

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
