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
