-- Knowledge Forge storage schema.
--
-- Every table's primary key is the model's deterministic stable_id (see
-- knowledge_forge.ids), so re-running the pipeline on unchanged input
-- reproduces the same ids and every INSERT below is a safe upsert. The one
-- exception is `jobs` below: a job is a queue event, not content, so its id
-- is a random uuid4 rather than a stable_id.

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
    project_id    TEXT NOT NULL DEFAULT 'default'
);
ALTER TABLE documents ADD COLUMN IF NOT EXISTS project_id TEXT NOT NULL DEFAULT 'default';
CREATE INDEX IF NOT EXISTS idx_documents_source_id ON documents(source_id);
CREATE INDEX IF NOT EXISTS idx_documents_project_id ON documents(project_id);

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
    page_end        INT
);
CREATE INDEX IF NOT EXISTS idx_chunks_document_id ON chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_source_id ON chunks(source_id);

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
