# 임베딩 (pgvector)

`KnowledgeObject`(기본) 또는 원시 `Chunk`(`KF_EMBEDDING_SOURCE=chunk`, 아래 "청크 기반 vs
KnowledgeObject 기반" 절)를 벡터로 만들어 PostgreSQL(pgvector)에 저장하는 기능. `wiki/agent-search.md`가
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
  `storage/store.py`의 `record_embeddings()`/`record_chunk_embeddings()`가 이 필드 이름에
  그대로 의존하므로 임의로 바꾸면 안 된다. `target_kind`에 따라 `knowledge_object_id` 또는
  `chunk_id` 중 정확히 하나만 채워야 하며(`__post_init__`이 검증, 테이블 CHECK 제약이 2차
  방어), `document_id`는 항상 채운다. `embedding_id`는 `stable_id(target_kind, 대상 id,
  embedding_model)`로 계산되는 결정적 surrogate PK — 같은 대상을 같은 모델로 다시 임베딩하면
  새 행이 아니라 제자리 upsert가 된다.
- `embedding/factory.py`의 `get_embedding_provider()`가 `KF_EMBEDDING_PROVIDER`를 보고 구현체를
  고르는 유일한 분기점 — `extraction/factory.py::get_extractor()`와 같은 스위치 포인트 패턴이다.
  `"openai"`(기본)와 `"openrouter"` 두 개가 구현되어 있다(아래 "OpenRouter / OpenAI 호환
  엔드포인트" 절).
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
  "acme"라는 일반 단어)이기 때문이다. 청크 grain용 `build_chunk_embedding_text()`는 같은
  규칙으로 `chunk.text`만 정규화한다(요약/토픽이 없다는 게 이 모드의 본질이다).
- `EmbeddingError`(`exceptions.py`)는 `KnowledgeExtractionError`와 같은 모양의 non-retryable
  예외 — 배치 임베딩 호출이 실패했을 때 던진다.

## 환경변수

| 환경변수 | 기본값 | 설명 |
|---|---|---|
| `KF_EMBEDDING_ENABLED` | `False` | `extract`가 기본적으로 임베딩을 생성할지. `--store`처럼 opt-in(꺼져 있으면 저장은 되지만 임베딩은 안 함) |
| `KF_EMBEDDING_PROVIDER` | `openai` | 임베딩 백엔드 선택 스위치: `openai` 또는 `openrouter` |
| `KF_EMBEDDING_MODEL` | `text-embedding-3-small` | 실제 호출할 임베딩 모델명. provider마다 형식이 다르다 — `openai`는 `text-embedding-3-small`, `openrouter`는 `openai/text-embedding-3-small` 같은 `vendor/model` 슬러그 |
| `KF_EMBEDDING_API_KEY` | (없음) | 임베딩 API 키. **이 저장소에서 `Settings`에 들어간 유일한 자격 증명이며, `.env`에 적어서 실제로 동작하는 유일한 키다** — 아래 "왜 `KF_EMBEDDING_API_KEY`라는 예외를 뒀는가" 참고. provider에 따라 OpenAI 키 또는 OpenRouter 키를 넣는다 |
| `KF_EMBEDDING_BASE_URL` | (없음 → provider 기본값) | 엔드포인트 override. 비우면 `openai`는 `api.openai.com`, `openrouter`는 `KF_OPENROUTER_BASE_URL`. 로컬 vLLM/TEI처럼 OpenAI 호환 서버를 붙일 때 provider는 `openai`로 두고 이 값만 바꾸면 된다 |
| `KF_EMBEDDING_SOURCE` | `knowledge_object` | 무엇을 임베딩할지: `knowledge_object`(추출된 KO의 summary/content/topics) 또는 `chunk`(원시 청크 텍스트, 추출 없이도 가능). `extract`/`embed`의 `--embedding-source`로 실행마다 override 가능 — 아래 "청크 기반 vs KnowledgeObject 기반" |
| `KF_EMBEDDING_DIMENSIONS` | `1536` | `schema.sql`의 `vector(N)` 차원과 반드시 일치해야 한다 — 다른 차원의 모델로 바꾸려면 마이그레이션이 필요(아래 "왜 별도 테이블인가" 참고) |
| `OPENAI_API_KEY` / `OPENROUTER_API_KEY` | (없음) | `KF_EMBEDDING_API_KEY`가 비었을 때의 마지막 fallback(활성 provider에 맞는 쪽만 본다). `KF_` 접두가 아니라 `os.environ`에서 직접 읽으므로 **셸에서 export 해야만 읽힌다 — `.env`에 적으면 아무 효과가 없다** |

임베딩 API 동시 요청 수는 별도 설정 없이 기존 `KF_MAX_CONCURRENT_LLM_REQUESTS`(기본 5)를
그대로 재사용한다 — "동시 요청을 무한정 fan-out하지 않는다"는 같은 관심사라, 전용 설정을 따로
둘 만큼의 근거가 아직 없다는 판단.

### 왜 `KF_EMBEDDING_API_KEY`라는 예외를 뒀는가

`config.py`의 `Settings` docstring은 "API 키는 `Settings`에 중복 저장하지 않는다"를 원칙으로
못박아 뒀다(Anthropic/OpenRouter/OpenAI 모두 provider 클래스가 환경에서 직접 읽는다).
`embedding_api_key`는 그 원칙의 **유일한 예외**이고, 취향이 아니라 기계적인 이유가 있다:

- pydantic-settings는 `.env`를 **`Settings` 인스턴스로만** 읽는다. 그 값들을 `os.environ`에
  주입하지 않으며, 이 저장소 어디에도 `load_dotenv()` 호출이 없다.
- provider 클래스들은 키를 `os.environ.get("OPENAI_API_KEY")`로 읽는다. 즉 `.env`에
  `OPENAI_API_KEY=sk-...`를 적어두면 **아무도 그 줄을 읽지 않는다** — 조용히 무시되고, 첫
  호출에서 401이 난다. 실제로 동작하려면 셸에서 `export` 해야 한다.
- "임베딩 키를 `.env`에 넣고 싶다"는 요구는 정당하고 흔한데, 위 구멍 때문에 그게 불가능했다.
  `KF_` 접두가 붙은 키를 하나 두면 pydantic-settings가 `.env`에서 읽어 주므로, **`.env` 하나로
  임베딩을 설정할 수 있게 하려면 키 하나를 `Settings`에 올리는 수밖에 없다.**

두 provider의 자격 증명 우선순위는 동일하다:

1. 생성자 인자 `api_key=` (테스트/프로그램적 주입)
2. `settings.embedding_api_key` (= `.env`의 `KF_EMBEDDING_API_KEY`)
3. `os.environ`의 `OPENAI_API_KEY`(openai) / `OPENROUTER_API_KEY`(openrouter) — 기존 동작
   하위호환. 이미 export 해서 쓰던 환경은 아무것도 바꾸지 않아도 그대로 돌아간다.

2가 3보다 앞서는 건 의도적이다 — 셸에 남아 있는 오래된 키가 방금 `.env`에 적은 키를 가리면
디버깅이 불가능해진다. 이 우선순위와 ".env의 비-KF_ 키는 아무 효과가 없다"는 사실은
`tests/embedding/test_embedding_settings.py`가 고정해 두었다(누군가 `load_dotenv()`를 넣으면
그 테스트가 깨지면서 이 절을 다시 보게 된다).

### OpenRouter / OpenAI 호환 엔드포인트

`KF_EMBEDDING_PROVIDER=openrouter`는 OpenRouter의 OpenAI 호환 `/embeddings`
(`POST https://openrouter.ai/api/v1/embeddings`, Bearer 인증)를 호출한다 —
`embedding/openrouter_provider.py`. 프로토콜이 OpenAI와 같아서 `openai` SDK에 `base_url`과
키만 바꿔 끼운 형태이고(`extraction/openrouter_extractor.py`와 같은 방식), 동기는 비용이 아니라
**모델 선택의 유연성**이다([`llm-providers.md`](llm-providers.md)와 같은 이유).

```bash
# .env 만으로 설정 (export 불필요)
KF_EMBEDDING_ENABLED=true
KF_EMBEDDING_PROVIDER=openrouter
KF_EMBEDDING_MODEL=openai/text-embedding-3-small
KF_EMBEDDING_API_KEY=sk-or-...
KF_EMBEDDING_DIMENSIONS=1536
```

`OpenAIEmbeddingProvider`와 다른 점 두 가지:

- **`dimensions` 파라미터를 보내지 않는다.** OpenAI API는 text-embedding-3-* 계열에서 이 값을
  받지만 OpenRouter의 `/embeddings` 문서엔 없다. 게이트웨이가 라우팅하는 모델마다 무시할지
  400을 낼지 알 수 없으므로, 차원은 *요청*하지 않고 *검증*만 한다(아래).
- **응답을 `index`로 재정렬한다.** OpenAI 본진 API는 `data`가 입력 순서라는 계약을 보장하지만,
  OpenRouter는 남의 응답을 중계하는 게이트웨이라 그 보장이 간접적이다. `index` 필드가 모두
  있으면 그걸로 정렬하고, 없으면 위치 순서로 fallback한다. 순서가 어긋나면 벡터가 **엉뚱한
  KnowledgeObject/Chunk에 조용히 붙기** 때문에(파이프라인이 입력과 위치로 zip한다) 방어가
  필요한 지점이다.

`KF_EMBEDDING_BASE_URL`은 `openai` provider에도 적용된다 — 로컬 vLLM/TEI/LM Studio처럼 OpenAI
호환 `/embeddings`를 제공하는 서버는 provider를 `openai`로 둔 채 base_url만 바꿔 붙이면 되고,
세 번째 provider 클래스를 만들 이유가 없다(`agent_openai_compatible_base_url`이 에이전트
런타임에 존재하는 이유와 같다).

### 차원 가드 (`embedding/validation.py`)

`embed()`는 반환 직전에 두 가지를 검사하고, 어긋나면 `EmbeddingError`를 던진다:

1. **개수** — 입력 텍스트 수와 벡터 수가 다르면 중단. 위치로 zip하는 호출자(`pipeline.py`)에게
   짧은 응답은 "실패"가 아니라 "잘못된 짝짓기"로 조용히 흘러가기 때문이다.
2. **차원** — 첫 벡터의 길이가 `KF_EMBEDDING_DIMENSIONS`와 다르면 중단. 이 값은
   `storage/schema.sql`의 `knowledge_object_embeddings.embedding vector(N)`과 반드시 같아야
   하는데, 어기면 INSERT 시점에 원인에서 몇 겹 떨어진 pgvector 에러("expected 1536 dimensions,
   not 3072")로만 드러난다. OpenRouter를 붙이면서 이 실수가 **설정 한 줄 거리**가 됐다 —
   `openai/text-embedding-3-large`(3072), `qwen/qwen3-embedding-8b`(4096) 같은 모델이 흔하다.
   그래서 모델명·기대 차원·실제 차원·"schema.sql도 같이 고쳐야 한다"를 한 번에 알려주는 한국어
   에러를 provider 경계에서 던진다. 한 번의 API 호출은 한 모델의 출력이라 첫 벡터만 재면 된다.

## 청크 기반 vs KnowledgeObject 기반 — `KF_EMBEDDING_SOURCE`

임베딩할 grain은 둘 중 하나다. 한 실행에 하나만 고르며(둘 다 켜면 같은 문서가 `vector_search`
랭킹에 두 번 등장한다), 실행마다 `extract --embedding-source` / `embed --embedding-source`로
바꿀 수 있다.

| | `knowledge_object` (기본) | `chunk` |
|---|---|---|
| 임베딩 텍스트 | KO의 `summary` + `content` 값 + `topics` | 청크 원문 `chunk.text` |
| 생성 시점 | 추출 → dedup/supersedes 병합이 끝난 뒤 | 청킹 직후, **추출 전**(추출을 아예 안 해도 됨) |
| 정밀도 | 높음(LLM이 정리한 텍스트, 중복 제거됨) | 낮음(잡음, 중복 미제거) |
| 상한 | 추출 품질에 갇힘 — 추출이 놓친 지식은 검색 불가 | 없음 — 원문에 있으면 후보가 됨 |
| 언제 쓰나 | 기본 | 추출이 너무 느리거나 비싼 소스, 또는 KO 생성을 건너뛰는 실행(`extract --no-extract`) |

`extract --no-extract`는 파이프라인을 청킹/저장에서 멈추고 KO를 만들지 않는다 — "KnowledgeObject는
선택"인 경로. 이때 `--embed --embedding-source chunk`를 같이 줘야 문서가 벡터 검색 대상이
된다(`knowledge_object` 모드는 임베딩할 KO가 없으니 경고만 내고 아무것도 임베딩하지 않는다).
LLM 자격 증명 검사도 건너뛴다 — 임베딩 API 키(`OPENAI_API_KEY`)만 있으면 된다.

두 모드의 행은 같은 테이블에 `target_kind`로 구분돼 들어가고, `vector_search`는 기본적으로
둘을 한 랭킹에서 경쟁시킨다(같은 모델의 같은 벡터 공간이라 섞어도 된다). 한 grain만 따로
보려면 `search/service.py::vector_search(target_kind=...)`를 명시한다 — 에이전트 도구는
노출하지 않고, 평가([`search-evaluation.md`](search-evaluation.md) (C))처럼 "KO 모드 vs 청크
모드"를 A/B할 때 쓰라고 둔 파라미터다. 청크 grain 히트는 `type="chunk"`, `summary`=원문 앞
500자(`postgres_store.py::_CHUNK_HIT_SUMMARY_CHARS` — 청크는 `--max-tokens`만큼 길 수 있어
`limit`개를 통째로 실으면 에이전트 컨텍스트가 터진다), `topics=[]`로 렌더링되고, 모든 히트에
`target_kind`/`document_id`/`knowledge_object_id`/`chunk_id`(둘 중 하나는 None)가 실린다.

## 스키마: `knowledge_object_embeddings` (`storage/schema.sql`)

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS knowledge_object_embeddings (
    embedding_id        TEXT PRIMARY KEY,           -- stable_id(target_kind, 대상 id, embedding_model)
    target_kind         TEXT NOT NULL CHECK (target_kind IN ('knowledge_object', 'chunk')),
    knowledge_object_id TEXT REFERENCES knowledge_objects(knowledge_object_id),  -- target_kind='knowledge_object'일 때만
    chunk_id            TEXT REFERENCES chunks(chunk_id),                        -- target_kind='chunk'일 때만
    document_id         TEXT NOT NULL REFERENCES documents(document_id),         -- 두 grain이 공유하는 키, 항상
    embedding_model     TEXT NOT NULL,
    embedding           vector(1536) NOT NULL,
    embedded_text       TEXT NOT NULL,
    embedded_text_hash  TEXT NOT NULL,
    project_id          TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL,
    CHECK ((target_kind = 'knowledge_object' AND knowledge_object_id IS NOT NULL AND chunk_id IS NULL)
        OR (target_kind = 'chunk' AND chunk_id IS NOT NULL AND knowledge_object_id IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_koe_ko_model
    ON knowledge_object_embeddings(knowledge_object_id, embedding_model) WHERE knowledge_object_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_koe_chunk_model
    ON knowledge_object_embeddings(chunk_id, embedding_model) WHERE chunk_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_koe_embedding_hnsw
    ON knowledge_object_embeddings USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_koe_project_id ON knowledge_object_embeddings(project_id);
CREATE INDEX IF NOT EXISTS idx_koe_document_id ON knowledge_object_embeddings(document_id);
CREATE INDEX IF NOT EXISTS idx_koe_target_kind ON knowledge_object_embeddings(target_kind);
```

테이블 이름은 청크 grain이 추가된 뒤에도 `knowledge_object_embeddings` 그대로다 — 이름을
바꾸면 `search/sql.py`의 스키마 설명, 문서, 기존 DB까지 전부 따라가야 하는데 그만한 값어치가
없다고 판단했다.

### 왜 `knowledge_objects`/`chunks`에 컬럼을 얹지 않고 별도 테이블인가

임베딩 벡터 하나를 `knowledge_objects.embedding`처럼 컬럼으로 얹었다면, 임베딩 모델을 바꾸는
일(예: `text-embedding-3-small` → 다른 차원의 모델)이 곧 스키마 변경(컬럼 재정의 + 기존 값
백필)이 된다. 대신 `(대상, embedding_model)`을 유일 키로 잡아, 모델을 바꾸는 것을 **새 행을
추가하는 것**으로 만들었다 — 옛 모델의 벡터와 새 모델의 벡터가 같은 테이블에 나란히 남아 있을
수 있어, 롤아웃 중에도 두 모델을 동시에 조회 가능한 상태로 유지할 수 있다.

### 왜 surrogate PK + `document_id`인가

원래 PK는 `(knowledge_object_id, embedding_model)` 복합키였다. 청크 grain이 들어오면서
`knowledge_object_id`가 NULL일 수 있게 됐고, NULL 가능한 두 컬럼(`knowledge_object_id` XOR
`chunk_id`)에 걸친 복합 PK는 만들 수 없다 — 그래서 결정적 surrogate `embedding_id`를 PK로
두고, grain별 유일성은 partial unique index 두 개(`idx_koe_ko_model`/`idx_koe_chunk_model`)로
예전 PK가 하던 일을 그대로 보장한다. `embedding_id`가 `stable_id(...)`라 upsert 의미도 그대로다.

`document_id`를 두 grain 모두에 항상 채우는 이유는 검색 평가 때문이다: KO 모드 히트와 청크
모드 히트는 서로 다른 id 공간에 있어 그대로는 비교할 수 없지만, 둘 다 `document_id`를
가지므로 "이 질의의 정답 문서를 찾았는가"는 같은 골든셋으로 채점할 수 있다 — 그래야 두 모드를
공정하게 A/B할 수 있다([`search-evaluation.md`](search-evaluation.md) (C)). 스키마에서 이
컬럼을 뺐다면 청크 히트는 `chunks`, KO 히트는 `knowledge_objects`를 각각 join해야 문서를 알 수
있어 평가/검색 양쪽 SQL이 grain별로 갈라진다.

**기존 DB 마이그레이션**: `init_schema()`가 `target_kind` 컬럼이 없는 옛 모양의 테이블을 발견하면
`DROP` 후 재생성한다(`schema.sql`의 `DO $$ ... $$` 블록). 임베딩은 파생 데이터라
`knowledge-forge embed`로 다시 채우면 되고, `CREATE TABLE IF NOT EXISTS`로는 PK를 바꿀 수
없어서다. 이미 임베딩이 많이 쌓인 DB라면 이 재생성 뒤 백필을 한 번 돌려야 한다.

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
임베딩해 나중에 버려지는 낭비를 피하기 위해서다. `--embedding-source chunk`면 대신
`store.record_chunks()` 직후에 `store.record_chunk_embeddings(...)`를 호출하고 KO는 임베딩하지
않는다.

```bash
# 추출 없이 청크만 저장 + 임베딩 (KO 선택 생략 경로)
uv run knowledge-forge extract ./big-dump/ --store --no-extract --embed --embedding-source chunk
```

### 이미 저장된 KO/청크를 나중에 임베딩 — `knowledge-forge embed` 백필

```bash
export KF_DATABASE_URL="postgresql://kf:kf@localhost:15432/knowledge_forge"
export OPENAI_API_KEY=sk-...
uv run knowledge-forge embed --project-id my-project --batch-size 64
uv run knowledge-forge embed --project-id my-project --embedding-source chunk   # 청크 grain 백필
```

`KF_EMBEDDING_ENABLED`를 나중에 켰거나, `extract --no-embed`로 저장만 해뒀던 KnowledgeObject를
소급 임베딩할 때 쓴다. `PostgresStore.list_knowledge_objects_missing_embedding(embedding_model,
project_id, limit)`이 `knowledge_objects` LEFT JOIN `knowledge_object_embeddings`로 **해당
`embedding_model`에 대해 임베딩 행이 아예 없는 KO만** 찾아 배치 단위로 돌며 채운다.
`--embedding-source chunk`면 같은 모양의 `list_chunks_missing_embedding()`이 `chunks` 기준으로
돈다(청크는 `project_id`를 갖지 않아 `sources`를 join해 `(chunk, project_id)` 쌍으로 돌려준다).
`worker` 명령처럼 `KF_DATABASE_URL`이 없으면 에러로 종료한다.

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
# (또는 export 없이 .env에 KF_EMBEDDING_API_KEY=sk-... 한 줄 — 위 "왜 KF_EMBEDDING_API_KEY라는
#  예외를 뒀는가" 절. .env의 OPENAI_API_KEY는 읽히지 않는다.)

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

- `tests/embedding/` — `test_embedding_text.py`(`build_embedding_text()`/
  `build_chunk_embedding_text()` 단위 테스트), `test_embedding_record.py`(grain XOR 검증과
  `embedding_id` 결정성), `test_openai_embedding_provider.py`/
  `test_openrouter_embedding_provider.py`(OpenAI SDK 클라이언트를 모킹한 provider 단위 테스트 —
  자격 증명 우선순위 3단계, base_url override, 배치 순서/`index` 재정렬, 개수·차원 불일치 에러,
  401 한국어 힌트), `test_embedding_factory.py`(`KF_EMBEDDING_PROVIDER` 분기),
  `test_embedding_settings.py`(`.env`에서 `KF_EMBEDDING_API_KEY`가 읽히는지 + 비-KF_ 키는
  `os.environ`에 들어오지 않는지). [`llm-providers.md`](llm-providers.md)의 "검증 상태"
  절과 같은 철학 — 이 환경엔 `OPENAI_API_KEY`/`OPENROUTER_API_KEY`가 없어 라이브 검증은 못했다.
- `tests/storage/test_postgres_store.py` — `test_save_embeddings_upsert_is_idempotent`(같은
  대상+모델로 다시 저장하면 갱신되고 행이 늘지 않는지), `test_list_knowledge_objects_missing_embedding`
  / `test_list_chunks_missing_embedding_filters_by_model_and_project`(모델별/프로젝트별 필터링),
  `test_search_by_embedding_mixes_grains_by_default_and_narrows_on_target_kind`(두 grain 혼합
  랭킹, `target_kind` 필터, 히트 모양), `test_schema_rejects_a_row_whose_ids_do_not_match_its_target_kind`
  (CHECK 제약), `test_init_schema_migrates_the_pre_chunk_embeddings_table_in_place`(옛 PK 모양
  테이블의 자동 재생성). `KF_TEST_DATABASE_URL`이 없으면 자동 skip([`storage.md`](storage.md)
  "테스트" 절과 동일한 관례). "임베딩 없는 대상" 스캔 테스트는 `limit`에 가려지지 않도록 테스트
  전용 `project_id`로 범위를 좁힌다 — 데이터가 쌓인 개발 DB에서도 돌아가야 해서.
- `tests/test_pipeline_dedup_and_supersedes.py` — `embedder`가 주어지면 문서당 병합이 끝난
  **최종** `merged_outcome.objects`로, 저장 이후 시점에 정확히 호출되는지(`embedder=None`이면
  `store`가 있어도 호출되지 않는지), `embedding_source="chunk"`면 추출 전에 청크만 임베딩하고
  KO는 임베딩하지 않는지, `extractor=None`이면 추출을 건너뛰고도 청크 저장/임베딩과 문서당 빈
  outcome yield가 되는지를 가짜 `EmbeddingProvider`/`PipelineStore`로 검증한다.
