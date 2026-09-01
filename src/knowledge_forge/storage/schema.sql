-- Knowledge Forge storage schema.
--
-- Every table's primary key is the model's deterministic stable_id (see
-- knowledge_forge.ids), so re-running the pipeline on unchanged input
-- reproduces the same ids and every INSERT below is a safe upsert.

CREATE TABLE IF NOT EXISTS sources (
    source_id     TEXT PRIMARY KEY,
    source_type   TEXT NOT NULL,
    uri           TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    raw_path      TEXT,
    collected_at  TIMESTAMPTZ NOT NULL,
    modified_at   TIMESTAMPTZ,
    metadata      JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS documents (
    document_id   TEXT PRIMARY KEY,
    source_id     TEXT NOT NULL REFERENCES sources(source_id),
    elements      JSONB NOT NULL,
    metadata      JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_documents_source_id ON documents(source_id);

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

CREATE TABLE IF NOT EXISTS knowledge_objects (
    knowledge_object_id TEXT PRIMARY KEY,
    source_id           TEXT NOT NULL REFERENCES sources(source_id),
    document_id         TEXT NOT NULL REFERENCES documents(document_id),
    chunk_id            TEXT NOT NULL REFERENCES chunks(chunk_id),
    summary              TEXT NOT NULL,
    facts                 JSONB NOT NULL DEFAULT '[]'::jsonb,
    entities               JSONB NOT NULL DEFAULT '[]'::jsonb,
    relations               JSONB NOT NULL DEFAULT '[]'::jsonb,
    topics                  JSONB NOT NULL DEFAULT '[]'::jsonb,
    document_type           TEXT NOT NULL,
    extracted_at             TIMESTAMPTZ NOT NULL,
    model_used               TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ko_source_id ON knowledge_objects(source_id);
CREATE INDEX IF NOT EXISTS idx_ko_document_id ON knowledge_objects(document_id);
CREATE INDEX IF NOT EXISTS idx_ko_chunk_id ON knowledge_objects(chunk_id);
