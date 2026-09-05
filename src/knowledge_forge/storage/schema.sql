-- Knowledge Forge storage schema.
--
-- Every table's primary key is the model's deterministic stable_id (see
-- knowledge_forge.ids), so re-running the pipeline on unchanged input
-- reproduces the same ids and every INSERT below is a safe upsert. The one
-- exception is `jobs` below: a job is a queue event, not content, so its id
-- is a random uuid4 rather than a stable_id.

-- pgvector extension for knowledge_object_embeddings below (wiki/embeddings.md).
-- Requires a Postgres image that ships the extension (docker-compose.yml's
-- postgres service uses pgvector/pgvector:pg16) - CREATE EXTENSION fails
-- against a plain postgres image.
CREATE EXTENSION IF NOT EXISTS vector;
-- pg_trgm for the document-title / chunk-text lexical indexes below
-- (search/service.py's document_search/lexical_search). Ships with every
-- stock Postgres (contrib), so unlike `vector` this needs no special image.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS sources (
    source_id     TEXT PRIMARY KEY,
    source_type   TEXT NOT NULL,
    uri           TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    raw_path      TEXT,
    collected_at  TIMESTAMPTZ NOT NULL,
    modified_at   TIMESTAMPTZ,
    metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,
    project_id    TEXT NOT NULL DEFAULT 'default'
);
-- ALTER ... IF NOT EXISTS covers databases created before project_id existed;
-- CREATE TABLE IF NOT EXISTS above is a no-op against them.
ALTER TABLE sources ADD COLUMN IF NOT EXISTS project_id TEXT NOT NULL DEFAULT 'default';
CREATE INDEX IF NOT EXISTS idx_sources_project_id ON sources(project_id);

CREATE TABLE IF NOT EXISTS documents (
    document_id   TEXT PRIMARY KEY,
    source_id     TEXT NOT NULL REFERENCES sources(source_id),
    elements      JSONB NOT NULL,
    metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,
    project_id    TEXT NOT NULL DEFAULT 'default',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE documents ADD COLUMN IF NOT EXISTS project_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE documents ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE documents ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
CREATE INDEX IF NOT EXISTS idx_documents_source_id ON documents(source_id);
CREATE INDEX IF NOT EXISTS idx_documents_project_id ON documents(project_id);
-- Denormalized display/search title (storage/document_title.py derives it:
-- parser metadata title -> source name/subject -> slack channel+date ->
-- first heading). `documents.metadata->>'title'` alone is NULL for nearly
-- every SaaS-collected row (the parsers don't know the Drive file name or
-- mail subject - only `sources.metadata` does), so title search needs one
-- materialized column rather than a per-query CASE over two JSONB blobs.
-- Nullable: a document with no usable title anywhere simply can't be found
-- by title (it stays reachable through lexical_search/vector_search).
-- Trigram GIN over it: pg_trgm serves both `ILIKE '%..%'` substring and
-- `similarity()` fuzzy matching, and - unlike tsvector's 'simple'
-- dictionary - needs no tokenizer, which matters because the titles are
-- mostly Korean/underscore-joined file names a text search parser splits
-- badly (see wiki/search-architecture.md ③).
ALTER TABLE documents ADD COLUMN IF NOT EXISTS title TEXT;
CREATE INDEX IF NOT EXISTS idx_documents_title_trgm ON documents USING gin (title gin_trgm_ops);

-- documents.metadata->>'version_family' (models/document.py's DocumentMetadata.
-- version_family, parsed by ingest_version.py) is what every "다른 버전
-- 있어?" lookup and the PREVIOUS_VERSION relink (document_links.py) filter
-- on - an expression index rather than a real column so the JSONB
-- metadata blob stays the single place a Document's metadata lives.
CREATE INDEX IF NOT EXISTS idx_documents_version_family
    ON documents ((metadata->>'version_family'));

-- Document-to-document typed edges (models/link.py's DocumentLink /
-- LinkType, built by document_links.py). Distinct from `relations`, which
-- only ever connects KnowledgeObjects, and from the OKF `links/*.yaml`
-- view (pipeline._resolve_links) which is keyed by (source_id,
-- document_id) for the file tree - this table is keyed by document_id so
-- search/sql.py can JOIN it straight onto documents. link_id is
-- stable_id('document_link', link_type, from, to) so a re-run upserts
-- rather than duplicates. Both FKs are ON DELETE CASCADE: an edge without
-- its endpoint is meaningless, and deleting a document shouldn't need to
-- know about this table.
CREATE TABLE IF NOT EXISTS document_links (
    link_id           TEXT PRIMARY KEY,
    from_document_id  TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    to_document_id    TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    link_type         TEXT NOT NULL,
    confidence        DOUBLE PRECISION,
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_document_links_from ON document_links(from_document_id);
CREATE INDEX IF NOT EXISTS idx_document_links_to ON document_links(to_document_id);
CREATE INDEX IF NOT EXISTS idx_document_links_type ON document_links(link_type);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id        TEXT PRIMARY KEY,
    document_id     TEXT NOT NULL REFERENCES documents(document_id),
    source_id       TEXT NOT NULL REFERENCES sources(source_id),
    hierarchy_level TEXT NOT NULL,
    sequence_index  INT NOT NULL,
    parent_chunk_id TEXT,
    text            TEXT NOT NULL,
    token_count     INT NOT NULL,
    element_ids     JSONB NOT NULL DEFAULT '[]'::jsonb,
    page_start      INT,
    page_end        INT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
CREATE INDEX IF NOT EXISTS idx_chunks_document_id ON chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_source_id ON chunks(source_id);
-- Trigram GIN over the chunk body for lexical_search (error codes, ids,
-- exact phrases - things an embedding can't be "close" to). Same
-- pg_trgm-over-tsvector reasoning as idx_documents_title_trgm above.
CREATE INDEX IF NOT EXISTS idx_chunks_text_trgm ON chunks USING gin (text gin_trgm_ops);

-- `evidence` replaces the old single `chunk_id` column: a KnowledgeObject
-- can be grounded in more than one Chunk (see models/knowledge_object.py),
-- so provenance to chunks is a JSONB list of {chunk_id, ...} rather than a
-- single foreign key. `type`/`content` replace the old fixed
-- facts/entities/relations columns with the generic typed-envelope shape.
CREATE TABLE IF NOT EXISTS knowledge_objects (
    knowledge_object_id TEXT PRIMARY KEY,
    type                TEXT NOT NULL,
    source_id           TEXT NOT NULL REFERENCES sources(source_id),
    document_id         TEXT NOT NULL REFERENCES documents(document_id),
    evidence            JSONB NOT NULL DEFAULT '[]'::jsonb,
    summary              TEXT NOT NULL,
    content              JSONB NOT NULL DEFAULT '{}'::jsonb,
    topics               JSONB NOT NULL DEFAULT '[]'::jsonb,
    document_type        TEXT NOT NULL,
    extracted_at          TIMESTAMPTZ NOT NULL,
    model_used            TEXT NOT NULL,
    version               INT NOT NULL DEFAULT 1,
    pipeline_version      TEXT NOT NULL,
    source_hash           TEXT NOT NULL,
    supersedes            TEXT
);
CREATE INDEX IF NOT EXISTS idx_ko_type ON knowledge_objects(type);
CREATE INDEX IF NOT EXISTS idx_ko_source_id ON knowledge_objects(source_id);
CREATE INDEX IF NOT EXISTS idx_ko_document_id ON knowledge_objects(document_id);

-- Relation: a typed edge between two KnowledgeObjects (models/relation.py).
-- Graph-DB-shaped on purpose (see wiki/architecture.md "Layer 4") - a
-- future Graph DB/search index materializes off this table without a
-- pipeline redesign.
CREATE TABLE IF NOT EXISTS relations (
    relation_id  TEXT PRIMARY KEY,
    type         TEXT NOT NULL,
    subject_id   TEXT NOT NULL REFERENCES knowledge_objects(knowledge_object_id),
    object_id    TEXT NOT NULL REFERENCES knowledge_objects(knowledge_object_id),
    label        TEXT,
    confidence   DOUBLE PRECISION,
    evidence     JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_relations_subject_id ON relations(subject_id);
CREATE INDEX IF NOT EXISTS idx_relations_object_id ON relations(object_id);
CREATE INDEX IF NOT EXISTS idx_relations_type ON relations(type);

-- One embedding vector per (target, embedding_model), where the target is
-- either a KnowledgeObject (target_kind = 'knowledge_object', the default
-- KF_EMBEDDING_SOURCE) or a raw Chunk (target_kind = 'chunk', for sources
-- where LLM extraction is too slow/expensive to gate retrieval on - see
-- wiki/embeddings.md "청크 기반 vs KnowledgeObject 기반"). A separate table
-- rather than a column on knowledge_objects/chunks so switching embedding
-- models is a new row, not a schema migration (old vectors stay queryable
-- side by side during a rollout). embedded_text/embedded_text_hash record
-- exactly what was embedded, for reproducibility and (future) staleness
-- detection when a re-extraction changes a KO's summary/content - see
-- wiki/embeddings.md for why staleness re-embedding isn't automated yet.
--
-- document_id is always set, whichever grain a row is - it is the one
-- identifier both grains share, so a consumer (vector_search hits, the
-- search-evaluation golden set in wiki/search-evaluation.md (C)) can score
-- KO-mode and chunk-mode retrieval against the same document-level truth
-- and A/B the two modes fairly. project_id is denormalized from the owning
-- Source (same convention as documents.project_id) since a filtered ANN
-- query can't join out to sources cheaply. Dimension (1536) matches the
-- default embedding model, KF_EMBEDDING_MODEL=text-embedding-3-small -
-- changing models to a different dimension requires a migration.
--
-- embedding_id is stable_id(target_kind, target id, embedding_model) - a
-- deterministic surrogate PK, because a composite PK can't span two
-- nullable columns (knowledge_object_id XOR chunk_id). The two partial
-- unique indexes below enforce the per-grain uniqueness the old
-- (knowledge_object_id, embedding_model) PK used to.
--
-- One-time migration from the pre-chunk shape (PK on knowledge_object_id,
-- no target_kind column): the table is dropped and recreated below.
-- Embeddings are derivable (re-run `knowledge-forge embed`), and the
-- CREATE TABLE IF NOT EXISTS that follows can't alter an existing PK.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables WHERE table_name = 'knowledge_object_embeddings'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'knowledge_object_embeddings' AND column_name = 'target_kind'
    ) THEN
        DROP TABLE knowledge_object_embeddings;
    END IF;
END $$;
CREATE TABLE IF NOT EXISTS knowledge_object_embeddings (
    embedding_id        TEXT PRIMARY KEY,
    target_kind         TEXT NOT NULL CHECK (target_kind IN ('knowledge_object', 'chunk')),
    knowledge_object_id TEXT REFERENCES knowledge_objects(knowledge_object_id),
    chunk_id            TEXT REFERENCES chunks(chunk_id),
    document_id         TEXT NOT NULL REFERENCES documents(document_id),
    embedding_model     TEXT NOT NULL,
    embedding           vector(1536) NOT NULL,
    embedded_text       TEXT NOT NULL,
    embedded_text_hash  TEXT NOT NULL,
    project_id          TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL,
    CHECK (
        (target_kind = 'knowledge_object' AND knowledge_object_id IS NOT NULL AND chunk_id IS NULL)
        OR (target_kind = 'chunk' AND chunk_id IS NOT NULL AND knowledge_object_id IS NULL)
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_koe_ko_model
    ON knowledge_object_embeddings(knowledge_object_id, embedding_model)
    WHERE knowledge_object_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_koe_chunk_model
    ON knowledge_object_embeddings(chunk_id, embedding_model)
    WHERE chunk_id IS NOT NULL;
-- HNSW over IVFFlat: knowledge_objects is an append-heavy incremental
-- workload (pipeline/schedule/worker keep inserting), and HNSW doesn't need
-- a `lists` parameter tuned to table size or periodic rebuilds the way
-- IVFFlat does. Cosine distance matches how OpenAI-style embeddings are
-- normally compared. Read by search/service.py's vector_search
-- (postgres_store.py::search_by_embedding).
CREATE INDEX IF NOT EXISTS idx_koe_embedding_hnsw
    ON knowledge_object_embeddings USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_koe_project_id ON knowledge_object_embeddings(project_id);
CREATE INDEX IF NOT EXISTS idx_koe_document_id ON knowledge_object_embeddings(document_id);
CREATE INDEX IF NOT EXISTS idx_koe_target_kind ON knowledge_object_embeddings(target_kind);

-- Ingest job queue (wiki/api.md): the FastAPI `/ingest` endpoint enqueues a
-- row here and returns immediately; `knowledge-forge worker` polls it with
-- `FOR UPDATE SKIP LOCKED` and runs the actual pipeline. `job_type`
-- discriminates the payload shape so new source types (SaaS, once wired to
-- CLI/auth) add a new job_type instead of a new table.
CREATE TABLE IF NOT EXISTS jobs (
    job_id       TEXT PRIMARY KEY,
    job_type     TEXT NOT NULL,
    payload      JSONB NOT NULL DEFAULT '{}'::jsonb,
    status       TEXT NOT NULL DEFAULT 'pending',
    result       JSONB,
    error        TEXT,
    created_at   TIMESTAMPTZ NOT NULL,
    started_at   TIMESTAMPTZ,
    finished_at  TIMESTAMPTZ,
    project_id   TEXT NOT NULL DEFAULT 'default'
);
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS project_id TEXT NOT NULL DEFAULT 'default';
CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_project_id ON jobs(project_id);

-- Incremental-sync bookmark for the job-queue ingest path (ingest.py's
-- run_saas_ingest), keyed by `source` ("slack", "notion", ...) - one row per
-- SaaS source, holding whatever opaque shape that source's Collector.
-- get_cursor()/Cursor.data produces (see collectors/base.py). A single
-- SAAS_COLLECTOR_BUILDERS[source]() call already covers every channel/
-- resource that source's KF_* env config names in one Cursor (e.g.
-- SlackCollector's per-channel latest-ts map), so "one row per source name"
-- is the right granularity - not one row per channel. Distinct from
-- `schedule`'s in-memory cursor (scheduling/scheduler.py), which a
-- long-lived CLI process holds for its own lifetime instead of persisting.
CREATE TABLE IF NOT EXISTS sync_cursors (
    source       TEXT PRIMARY KEY,
    cursor_data  JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at   TIMESTAMPTZ NOT NULL
);

-- Search Evaluation / Feedback Loop (wiki/search-evaluation.md (A)) - an
-- event log, not Knowledge Graph content, so `log_id`/`feedback_id` are
-- uuid4 like `jobs.job_id` rather than a stable_id (see file header).
CREATE TABLE IF NOT EXISTS search_log (
    log_id        TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL DEFAULT 'default',
    query         TEXT NOT NULL,
    answer        TEXT NOT NULL,
    -- Raw tool-result payloads from every evidence-bearing tool call this
    -- turn made (file_search/vector_search/sql_search/plan_search) -
    -- [{tool, iteration, payload}, ...]. Kept as each tool's own native
    -- return shape rather than normalized into one {path, rank, score}
    -- schema - sql_search's {columns, rows} and plan_search's {subqueries,
    -- evidence} don't share file_search/vector_search's hit shape, and the
    -- LLM judge (search-evaluation.md (B)) reads this as free-form JSON
    -- context rather than a fixed schema.
    evidence      JSONB NOT NULL DEFAULT '[]'::jsonb,
    tool_calls    JSONB NOT NULL DEFAULT '[]'::jsonb,
    latency_ms    INT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_search_log_project_id ON search_log(project_id);
CREATE INDEX IF NOT EXISTS idx_search_log_created_at ON search_log(created_at);

CREATE TABLE IF NOT EXISTS search_feedback (
    feedback_id  TEXT PRIMARY KEY,
    log_id       TEXT NOT NULL REFERENCES search_log(log_id),
    rating       TEXT NOT NULL,
    comment      TEXT,
    created_at   TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_search_feedback_log_id ON search_feedback(log_id);

-- LLM-judge output (wiki/search-evaluation.md (B)) - one row per scored
-- search_log, produced by an offline `knowledge-forge eval` batch pass
-- (embeddings.md's `--embed`/`knowledge-forge embed` pattern), never
-- inline at answer time - no latency/cost added to the answer path.
CREATE TABLE IF NOT EXISTS eval_score (
    log_id              TEXT PRIMARY KEY REFERENCES search_log(log_id),
    groundedness_score  DOUBLE PRECISION NOT NULL,
    citation_precision  DOUBLE PRECISION,
    -- [{text, verdict: "supported"|"unsupported"|"contradicted", reason}, ...]
    -- - every claim, not just failing ones, so other aggregates (e.g. a
    -- future "contradicted" rate) don't need a second judge pass.
    claims              JSONB NOT NULL DEFAULT '[]'::jsonb,
    judge_model         TEXT NOT NULL,
    judged_at           TIMESTAMPTZ NOT NULL
);

-- Golden-set IR evaluation runs (wiki/search-evaluation.md (C)) - one
-- `eval_run` per `knowledge-forge eval-ir` invocation plus one
-- `eval_run_query` per golden query. Separate from `eval_score` (which keys
-- on live `search_log` traffic) because these score a fixed gold file with
-- no user in the loop, and the A/B comparison (`eval-ir --compare`) needs
-- the *per-query* metrics of both runs, not just the aggregate - hence the
-- child table rather than a single JSONB blob. run_id is uuid4 like
-- search_log.log_id (event row, not Knowledge Graph content).
CREATE TABLE IF NOT EXISTS eval_run (
    run_id          TEXT PRIMARY KEY,
    golden_version  TEXT NOT NULL,
    golden_path     TEXT NOT NULL,
    project_id      TEXT,
    -- {provider, model, limit, strategies_override, use_llm_constraints, k_values}
    config          JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- sha256(sorted-json(config))[:12] - a cheap "same settings?" key for
    -- grouping/comparing runs without diffing the JSON.
    config_hash     TEXT NOT NULL,
    -- Measured at run time, not copied from the gold file:
    -- {documents, chunks, knowledge_objects, embeddings_by_kind}. Two runs
    -- are only comparable when this matches (see ir_compare.py).
    index_snapshot  JSONB NOT NULL DEFAULT '{}'::jsonb,
    git_commit      TEXT,
    label           TEXT,
    started_at      TIMESTAMPTZ NOT NULL,
    finished_at     TIMESTAMPTZ,
    -- ir_metrics.aggregate() output ({overall, by_type, metric_n}) plus the
    -- run's error/stale query lists.
    summary         JSONB NOT NULL DEFAULT '{}'::jsonb
);
-- `git_commit`이 dirty였던 run의 `git status --porcelain` 줄 목록. 해시는
-- "달랐다"만 말해 주므로 *무엇이* 달랐는지는 여기서 읽는다. NULL은 두 가지
-- 뜻이다 - (a) 깨끗한 트리에서 돈 run, (b) 이 컬럼이 생기기 전의 옛 행(그
-- 26개 b470057 행처럼 dirty 여부 자체를 알 수 없다). (a)/(b)는 git_commit의
-- `+dirty.` 표식으로 구분한다(evaluation/ir_runner.py 참고).
ALTER TABLE eval_run ADD COLUMN IF NOT EXISTS dirty_files JSONB;
CREATE INDEX IF NOT EXISTS idx_eval_run_golden_version ON eval_run(golden_version);
CREATE INDEX IF NOT EXISTS idx_eval_run_started_at ON eval_run(started_at);

CREATE TABLE IF NOT EXISTS eval_run_query (
    run_id              TEXT NOT NULL REFERENCES eval_run(run_id) ON DELETE CASCADE,
    query_id            TEXT NOT NULL,
    query_text          TEXT NOT NULL,
    primary_type        TEXT NOT NULL,
    types               JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- Deduplicated document ranking the metrics were computed from, kept
    -- so a regression can be diagnosed without re-running retrieval.
    ranked_document_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    scores              JSONB NOT NULL DEFAULT '[]'::jsonb,
    predicted_count     INT,
    -- ir_metrics.score_query() output; empty when `error` is set.
    metrics             JSONB NOT NULL DEFAULT '{}'::jsonb,
    strategies          JSONB NOT NULL DEFAULT '[]'::jsonb,
    steps               JSONB NOT NULL DEFAULT '[]'::jsonb,
    elapsed_ms          DOUBLE PRECISION NOT NULL DEFAULT 0,
    -- Retrieval raised for this query; the run continues and the query
    -- simply contributes no metrics (it is listed in summary.errors).
    error               TEXT,
    -- DB가 채운다(러너가 넘기지 않는다). 같은 (run_id, query_id)를 다시 쓰는
    -- 건 재실행이 아니라 같은 run 안의 재시도/부분 저장이므로 created_at은
    -- 최초 기록, updated_at은 마지막 upsert 시각이다.
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, query_id)
);
-- 기존 DB용. 이 컬럼 이전의 옛 행은 둘 다 실제 기록 시각이 아니라 마이그레이션
-- 시각을 갖는다 - 그 run의 진짜 시각은 eval_run.started_at을 봐야 한다.
ALTER TABLE eval_run_query ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE eval_run_query ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- 에이전트 경로 평가(`eval-ir --mode agent`)가 채우는 열들. 파이프라인 경로
-- (`--mode pipeline`, 기본)에서는 retrieval_ms/total_ms만 차고 나머지는 NULL이다.
-- NULL은 "0이었다"가 아니라 **이 경로에서는 측정되지 않는 값**이라는 뜻이므로
-- 집계에서 0으로 채우지 말 것.
--
-- 왜 elapsed_ms를 그냥 두고 열을 더 붙이나: elapsed_ms에는 150개 run의 값이
-- 이미 들어 있고 그 의미는 "retrieve() 내부 측정치"다. 같은 열에 에이전트
-- 전체 시간을 섞으면 옛 행과 새 행이 다른 것을 재면서 같은 이름을 갖게 된다.
-- retrieval_ms가 elapsed_ms를 이어받고, total_ms가 새로 생기는 개념이다.
ALTER TABLE eval_run_query ADD COLUMN IF NOT EXISTS retrieval_ms  DOUBLE PRECISION;
ALTER TABLE eval_run_query ADD COLUMN IF NOT EXISTS generation_ms DOUBLE PRECISION;
ALTER TABLE eval_run_query ADD COLUMN IF NOT EXISTS total_ms      DOUBLE PRECISION;
ALTER TABLE eval_run_query ADD COLUMN IF NOT EXISTS llm_calls     INT;
ALTER TABLE eval_run_query ADD COLUMN IF NOT EXISTS tool_calls    INT;
-- 에이전트가 내놓은 최종 답변. LLM-judge(groundedness/citation)의 입력이고,
-- 점수가 떨어졌을 때 "검색이 못 찾았나 생성이 망쳤나"를 사람이 읽어 가르는 근거다.
ALTER TABLE eval_run_query ADD COLUMN IF NOT EXISTS answer        TEXT;
-- --repeats N > 1일 때 각 회차의 원본 기록([{metrics, timings, document_ids}, ...]).
-- `metrics` 열은 회차 평균이고 `metrics_std`가 표준편차다 - 평균만 남기면
-- "이 개선이 에이전트 노이즈보다 큰가"를 나중에 되물을 수 없다.
ALTER TABLE eval_run_query ADD COLUMN IF NOT EXISTS repeats       JSONB;
ALTER TABLE eval_run_query ADD COLUMN IF NOT EXISTS metrics_std   JSONB;
