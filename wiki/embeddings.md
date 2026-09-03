# 임베딩 (pgvector)

`KnowledgeObject`를 벡터로 만들어 PostgreSQL(pgvector)에 저장하는 기능. `wiki/agent-search.md`가
설계해 둔 "pgvector 스키마"와 "임베딩 생성 파이프라인"을 구현한 결과물이다 — 이 문서가 최신
상태의 기준이고, `agent-search.md`의 해당 절은 설계 당시 스케치라 실제 스키마와 사소하게 다를 수
있다. **검색 엔드포인트(`/search/vector` 등)와 MCP 서버는 이번 구현에 포함되지 않았다** — 아래
"포함 안 된 것" 절과 [`agent-search.md`](agent-search.md) 참고.

## 왜 `EmbeddingProvider`가 `Extractor`와 같은 패턴인가

Knowledge Extraction이 `Extractor` ABC(`extraction/base.py`) 뒤에서 Anthropic/OpenRouter/Claude
Agent SDK를 갈아 끼울 수 있게 만든 것과 똑같은 이유로, 임베딩도 `EmbeddingProvider` ABC
(`embedding/base.py`)를 두었다: 지금은 OpenAI 임베딩 API 하나뿐이지만, 나중에 다른 임베딩
백엔드(예: 로컬 모델, Voyage, Cohere)로 바꾸더라도 `pipeline.py`/`cli.py`가 구체 클래스를 몰라도
되게 하기 위해서다.

- `EmbeddingProvider.embed(texts: list[str]) -> list[list[float]]` 하나만 요구한다 — 텍스트
  하나가 아니라 리스트를 받는 이유는 [`llm-providers.md`](llm-providers.md)가 청크 묶음을 한
  번에 `Extractor.extract()`에 넘기는 이유와 같다: 임베딩 API도 요청당 과금/제한이 걸리므로,
  한 문서에서 나온 여러 `KnowledgeObject`를 한 번의 호출로 묶는 쪽이 텍스트 하나당 호출 한 번
  보다 싸고 빠르다.
- `EmbeddingRecord` dataclass(`embedding/base.py`)가 `knowledge_object_embeddings` 테이블 한
  행의 모양을 그대로 딴다 — `postgres_store.py`의 `save_embeddings()`와
  `storage/store.py`의 `record_embeddings()`가 이 필드 이름/순서에 그대로 의존하므로 임의로
  바꾸면 안 된다.
- `embedding/factory.py`의 `get_embedding_provider()`가 `KF_EMBEDDING_PROVIDER`를 보고 구현체를
  고르는 유일한 분기점 — `extraction/factory.py::get_extractor()`와 같은 스위치 포인트 패턴이다.
  지금은 `"openai"` 하나만 구현되어 있다(`config.py`의 `embedding_provider: Literal["openai"]`).
- `embedding/openai_provider.py`의 `OpenAIEmbeddingProvider`는
  `extraction/openrouter_extractor.py::OpenRouterExtractor`와 거의 같은 형태다: 지연 자격 증명
  해석(생성 시점에 키가 없어도 실패하지 않고, 실제 호출에서 401을 받으면 그때
  `EmbeddingError`로 감싼다), 인스턴스별 `asyncio.Semaphore`, `openai_retry()` 재사용(재시도
  정책은 [`llm-providers.md`](llm-providers.md)의 `retry.py` 절과 동일). 인증 실패 메시지는
  `OPENAI_EMBEDDING_CREDENTIAL_HINT_KO`.
- `embedding/text.py`의 `build_embedding_text()`가 `summary` + `content`의 값들 + `topics`를
  공백 정규화해 하나의 문자열로 합친다 — **대소문자는 보존한다**. `similarity.py::text_similarity()`가
  `.lower()`로 정규화하는 것과 의도적으로 다르다: difflib 문자 단위 diff에서 대소문자는 잡음일
  뿐이지만, 임베딩 모델은 자연어로 학습돼 있어 대소문자가 실제 신호("Acme"라는 고유명사 vs
  "acme"라는 일반 단어)이기 때문이다.
- `EmbeddingError`(`exceptions.py`)는 `KnowledgeExtractionError`와 같은 모양의 non-retryable
  예외 — 배치 임베딩 호출이 실패했을 때 던진다.

## 환경변수

| 환경변수 | 기본값 | 설명 |
|---|---|---|
| `KF_EMBEDDING_ENABLED` | `False` | `extract`가 기본적으로 임베딩을 생성할지. `--store`처럼 opt-in(꺼져 있으면 저장은 되지만 임베딩은 안 함) |
| `KF_EMBEDDING_PROVIDER` | `openai` | 임베딩 백엔드 선택 스위치. 지금은 `"openai"`만 구현됨 |
| `KF_EMBEDDING_MODEL` | `text-embedding-3-small` | 실제 호출할 임베딩 모델명 |
| `KF_EMBEDDING_DIMENSIONS` | `1536` | `schema.sql`의 `vector(N)` 차원과 반드시 일치해야 한다 — 다른 차원의 모델로 바꾸려면 마이그레이션이 필요(아래 "왜 별도 테이블인가" 참고) |
| `OPENAI_API_KEY` | (없음) | OpenAI API 키. `KF_` 접두사가 아니다 — `OPENROUTER_API_KEY`/`ANTHROPIC_API_KEY`와 같은 원칙으로, `Settings`에 중복 저장하지 않고 `OpenAIEmbeddingProvider` 생성 시점에 환경에서 직접 읽는다 |

임베딩 API 동시 요청 수는 별도 설정 없이 기존 `KF_MAX_CONCURRENT_LLM_REQUESTS`(기본 5)를
그대로 재사용한다 — "동시 요청을 무한정 fan-out하지 않는다"는 같은 관심사라, 전용 설정을 따로
둘 만큼의 근거가 아직 없다는 판단.

## 스키마: `knowledge_object_embeddings` (`storage/schema.sql`)

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS knowledge_object_embeddings (
    knowledge_object_id TEXT NOT NULL REFERENCES knowledge_objects(knowledge_object_id),
    embedding_model    TEXT NOT NULL,
    embedding          vector(1536) NOT NULL,
    embedded_text      TEXT NOT NULL,
    embedded_text_hash TEXT NOT NULL,
    project_id         TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (knowledge_object_id, embedding_model)
);
CREATE INDEX IF NOT EXISTS idx_koe_embedding_hnsw
    ON knowledge_object_embeddings USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_koe_project_id ON knowledge_object_embeddings(project_id);
```

### 왜 `knowledge_objects`에 컬럼을 얹지 않고 별도 테이블인가

임베딩 벡터 하나를 `knowledge_objects.embedding`처럼 컬럼으로 얹었다면, 임베딩 모델을 바꾸는
일(예: `text-embedding-3-small` → 다른 차원의 모델)이 곧 스키마 변경(컬럼 재정의 + 기존 값
백필)이 된다. 대신 복합 PK를 `(knowledge_object_id, embedding_model)`로 잡아, 모델을 바꾸는
것을 **새 행을 추가하는 것**으로 만들었다 — 옛 모델의 벡터와 새 모델의 벡터가 같은 테이블에
나란히 남아 있을 수 있어, 롤아웃 중에도 두 모델을 동시에 조회 가능한 상태로 유지할 수 있다.
`embedded_text`/`embedded_text_hash`는 정확히 무엇을 임베딩했는지 재현 가능하게 기록해 둔
것 — 지금 당장 읽는 코드는 없지만, 향후 staleness 감지(아래 절)에 쓰기 위해 미리 저장해
둔다. `project_id`는 `sources.project_id`/`documents.project_id`와 같은 관례로 소유
`Source`에서 역정규화한 값이다 — 필터링된 ANN(근사 최근접) 질의가 `sources`까지 join하지
않고 바로 `WHERE project_id = ...`로 좁힐 수 있게 하기 위해서다.

### 왜 HNSW + cosine인가

- **HNSW vs IVFFlat**: `knowledge_objects`는 파이프라인/스케줄/워커가 계속 insert하는
  append-heavy 워크로드다. IVFFlat은 `lists` 파라미터를 테이블 크기에 맞춰 튜닝해야 하고
  데이터가 늘어나면 주기적으로 재구축해야 한다. HNSW는 그런 튜닝/재구축 없이 점진적 삽입에
  잘 맞는다 — 이 프로젝트의 쓰기 패턴에 IVFFlat보다 적합하다고 판단했다.
- **cosine distance**: OpenAI류 임베딩을 비교할 때 일반적으로 쓰는 거리 함수라 그대로
  따랐다(`vector_cosine_ops`).
- 인덱스는 검색 엔드포인트가 아직 없는 지금도 테이블과 함께 미리 만들어 둔다 — 나중에
  검색 엔드포인트를 붙일 때 별도 마이그레이션(인덱스 생성은 대형 테이블에서 시간이 걸리는
  작업이다) 없이 바로 쓸 수 있게 하기 위해서다. 지금 이 인덱스를 실제로 읽는 쿼리는 아직 없다
  (`wiki/agent-search.md`의 `vector_search`가 여전히 설계만 있는 상태).

## 사용법

### 실시간 파이프라인에서 켜기 — `--embed`

```bash
export OPENAI_API_KEY=sk-...
uv run knowledge-forge extract tests/fixtures/sample.md --store --embed
```

`extract`의 `--embed/--no-embed` 플래그 기본값은 `KF_EMBEDDING_ENABLED`. `--store` 없이
`--embed`만 주면(`KF_DATABASE_URL` 없이 저장이 꺼진 상태) 경고만 출력하고 임베딩을 건너뛴다 —
임베딩을 쓸 곳(Postgres) 자체가 없기 때문이다.

파이프라인 내부에서는 `pipeline.run_pipeline()`이 `store.record_knowledge_objects()`/
`record_relations()` 직후, **문서 단위로 dedup/supersedes 병합까지 끝난 최종 객체 집합**
(`merged_outcome.objects`)에만 `store.record_embeddings(...)`를 호출한다 — 병합 전 중간 산출물을
임베딩해 나중에 버려지는 낭비를 피하기 위해서다.

### 이미 저장된 KO를 나중에 임베딩 — `knowledge-forge embed` 백필

```bash
export KF_DATABASE_URL="postgresql://kf:kf@localhost:15432/knowledge_forge"
export OPENAI_API_KEY=sk-...
uv run knowledge-forge embed --project-id my-project --batch-size 64
```

`KF_EMBEDDING_ENABLED`를 나중에 켰거나, `extract --no-embed`로 저장만 해뒀던 KnowledgeObject를
소급 임베딩할 때 쓴다. `PostgresStore.list_knowledge_objects_missing_embedding(embedding_model,
project_id, limit)`이 `knowledge_objects` LEFT JOIN `knowledge_object_embeddings`로 **해당
`embedding_model`에 대해 임베딩 행이 아예 없는 KO만** 찾아 배치 단위로 돌며 채운다. `worker`
명령처럼 `KF_DATABASE_URL`이 없으면 에러로 종료한다.

**주의: 이미 임베딩이 있는 KO는 이 명령으로 재임베딩되지 않는다** — 아래 staleness 절 참고.

## Staleness 재임베딩 미자동화 (의도적 스코프 축소)

재추출(re-extraction)로 같은 `source_id`의 `KnowledgeObject.summary`/`content`가 바뀌어도,
그 KO가 이미 `knowledge_object_embeddings`에 어떤 모델로든 행을 갖고 있다면 `knowledge-forge
embed`도, 파이프라인의 `--embed` 경로도 재임베딩하지 않는다. `list_knowledge_objects_missing_embedding()`이
"임베딩이 **아예 없는**" KO만 찾도록 짜여 있기 때문이다.

이건 실수로 빠뜨린 게 아니라 의도적으로 좁힌 스코프다:

- `embedded_text_hash` 컬럼은 이미 저장해 두고 있다 — "지금 저장된 임베딩이 최신 `summary`/
  `content`로부터 나온 게 맞는지"를 판단하려면 `build_embedding_text(ko)`를 다시 계산해 해시를
  비교하면 되고, 그 비교에 필요한 데이터는 이미 갖춰져 있다. 하지만 **그 비교를 수행하고
  다른 것으로 판명되면 재임베딩까지 트리거하는 로직은 아직 없다** — 컬럼만 미래를 위해 먼저
  깔아 둔 상태.
  - 이유는 `roadmap.md` 5단계의 "중복 제거는 같은 문서 안까지만 자동"/"`supersedes` 자동
    채우기는 `PostgresStore` 전용"과 같은 성격의 판단이다: staleness 재임베딩은 "한 번의
    파이프라인 실행 스트림 안"이 아니라 "이미 쌓인 Knowledge Base 전체를 훑어야 하는" 문제라,
    백그라운드 재조정(reconciliation) 잡 설계가 선행돼야 자연스럽다. 지금 이 구현의 스코프는
    "처음 임베딩을 만드는 것"까지고, "임베딩을 최신 상태로 계속 유지하는 것"은 별도 과제로
    남긴다.
  - `versioning.py::resolve_supersedes()`가 이미 재추출 시 이전 버전을 찾아 `supersedes`를
    채워 두므로, 필요하다면 "supersedes가 채워진 새 KO는 곧 재임베딩 대상"이라는 규칙으로
    나중에 연결할 수 있다 — 지금은 그 연결이 없다는 것만 명시해 둔다.

## 이번 구현에 포함되지 않은 것

pgvector 스키마와 임베딩 생성 파이프라인만 구현했다. [`agent-search.md`](agent-search.md)가
설계해 둔 다음 항목들은 **여전히 미구현**이다:

- 검색 API 엔드포인트: `/search/vector`, `/relations/expand`, `/okf/search`
- `knowledge-forge mcp-server` 명령
- Claude Code용 검색 skill

즉 지금은 벡터가 테이블에 쌓이기만 하고, 그 인덱스를 읽는 질의 경로가 아직 없다. `psql`로
직접 `ORDER BY embedding <=> '[...]'`를 날려보는 것은 가능하지만(아래 로컬 실행 예시), 정식
검색 엔드포인트는 다음 스코프다.

## 로컬에서 시도해보기

```bash
docker compose up -d postgres   # postgres 이미지가 pgvector/pgvector:pg16으로 바뀌어 있다
export KF_DATABASE_URL="postgresql://kf:kf@localhost:15432/knowledge_forge"
export KF_DATA_DIR="./data"
export OPENAI_API_KEY=sk-...    # 임베딩 API용 - Anthropic/OpenRouter 키와 별개

uv run knowledge-forge db init
uv run knowledge-forge extract tests/fixtures/sample.md --store --embed
# 또는: 이미 저장만 되어 있던 KO를 나중에 백필
uv run knowledge-forge embed
```

`psql`로 직접 확인:

```bash
psql "postgresql://kf:kf@localhost:15432/knowledge_forge" -c \
  "SELECT knowledge_object_id, embedding_model, embedded_text FROM knowledge_object_embeddings LIMIT 5;"
```

## 테스트

- `tests/embedding/`(신규) — `test_embedding_text.py`(`build_embedding_text()` 단위 테스트),
  `test_openai_embedding_provider.py`(OpenAI 클라이언트를 모킹한 `OpenAIEmbeddingProvider` 단위
  테스트). [`llm-providers.md`](llm-providers.md)의 "검증 상태" 절과 같은 철학 — 이 환경엔
  `OPENAI_API_KEY`가 없어 라이브 검증은 못했다.
- `tests/storage/test_postgres_store.py` — `test_save_embeddings_upsert_is_idempotent`(같은
  PK로 다시 저장하면 갱신되고 행이 늘지 않는지), `test_list_knowledge_objects_missing_embedding`
  (모델별/프로젝트별 필터링이 맞는지). `KF_TEST_DATABASE_URL`이 없으면 자동 skip([`storage.md`](storage.md)
  "테스트" 절과 동일한 관례).
- `tests/test_pipeline_dedup_and_supersedes.py` — `embedder`가 주어지면 문서당 병합이 끝난
  **최종** `merged_outcome.objects`로, 저장 이후 시점에 정확히 호출되는지(`embedder=None`이면
  `store`가 있어도 호출되지 않는지)를 가짜 `EmbeddingProvider`/`PipelineStore`로 검증한다.
