"""PostgreSQL persistence for Source/Document/Chunk/KnowledgeObject.

Every table's primary key is a deterministic stable_id (knowledge_forge.ids),
so every write here is an idempotent upsert - re-running the pipeline on
unchanged input is safe to repeat.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg
from pgvector import Vector
from psycopg.types.json import Jsonb

from knowledge_forge.models.chunk import Chunk
from knowledge_forge.models.document import Document
from knowledge_forge.models.knowledge_object import (
    DocumentRef,
    Evidence,
    KnowledgeObject,
)
from knowledge_forge.models.relation import Relation
from knowledge_forge.models.source import Source, SourceRef

if TYPE_CHECKING:
    from knowledge_forge.embedding.base import EmbeddingRecord

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


class PostgresStore:
    def __init__(self, dsn: str):
        self._dsn = dsn
        self._conn: psycopg.AsyncConnection | None = None

    async def connect(self) -> "PostgresStore":
        self._conn = await psycopg.AsyncConnection.connect(self._dsn, autocommit=True)
        await self._register_vector_types()
        return self

    async def _register_vector_types(self) -> None:
        """Teaches psycopg how to adapt `pgvector.Vector`/`numpy.ndarray`
        to/from the `vector` column type - required before any
        embedding-column read/write. Tolerates the `vector` extension not
        existing yet (a brand new database, before `init_schema()` has ever
        run) rather than raising: `connect()` itself must succeed against a
        fresh database so `init_schema()` can be reached at all to create
        the extension in the first place - this would otherwise be a
        chicken-and-egg failure on every first-time `knowledge-forge db init`.
        `init_schema()` calls this again once the extension actually exists."""
        from pgvector.psycopg import register_vector_async

        try:
            await register_vector_async(self._conn)
        except psycopg.ProgrammingError:
            pass

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> "PostgresStore":
        return await self.connect()

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    @property
    def conn(self) -> psycopg.AsyncConnection:
        if self._conn is None:
            raise RuntimeError("PostgresStore is not connected - use it as a context manager or call connect()")
        return self._conn

    async def init_schema(self) -> None:
        await self.conn.execute(_SCHEMA_PATH.read_text(encoding="utf-8"))
        # The schema above is what actually creates the `vector` extension
        # on a brand new database - `connect()`'s registration attempt was
        # a no-op if this is the first time this database has ever seen it,
        # so retry it now that the type genuinely exists.
        await self._register_vector_types()

    async def save_source(self, source: Source, raw_path: str | None) -> None:
        await self.conn.execute(
            """
            INSERT INTO sources (source_id, source_type, uri, content_hash, raw_path,
                                  collected_at, modified_at, metadata, project_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_id) DO UPDATE SET
                source_type = EXCLUDED.source_type,
                uri = EXCLUDED.uri,
                content_hash = EXCLUDED.content_hash,
                raw_path = EXCLUDED.raw_path,
                collected_at = EXCLUDED.collected_at,
                modified_at = EXCLUDED.modified_at,
                metadata = EXCLUDED.metadata,
                project_id = EXCLUDED.project_id
            """,
            (
                source.source_id,
                source.source_type.value,
                source.uri,
                source.content_hash,
                raw_path,
                source.collected_at,
                source.modified_at,
                Jsonb(source.metadata),
                source.project_id,
            ),
        )

    async def save_document(self, document: Document) -> None:
        await self.conn.execute(
            """
            INSERT INTO documents (document_id, source_id, elements, metadata, project_id)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (document_id) DO UPDATE SET
                source_id = EXCLUDED.source_id,
                elements = EXCLUDED.elements,
                metadata = EXCLUDED.metadata,
                project_id = EXCLUDED.project_id
            """,
            (
                document.document_id,
                document.source.source_id,
                Jsonb([e.model_dump(mode="json") for e in document.elements]),
                Jsonb(document.metadata.model_dump(mode="json")),
                document.source.project_id,
            ),
        )

    async def list_sources(
        self, project_id: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[Source]:
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT source_id, source_type, uri, content_hash, collected_at, modified_at,
                       metadata, project_id
                FROM sources
                WHERE (%s::text IS NULL OR project_id = %s)
                ORDER BY collected_at DESC
                LIMIT %s OFFSET %s
                """,
                (project_id, project_id, limit, offset),
            )
            rows = await cur.fetchall()
        return [
            Source(
                source_id=row[0],
                source_type=row[1],
                uri=row[2],
                content_hash=row[3],
                collected_at=row[4],
                modified_at=row[5],
                metadata=row[6],
                project_id=row[7],
            )
            for row in rows
        ]

    async def get_source(self, source_id: str) -> Source | None:
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT source_id, source_type, uri, content_hash, collected_at, modified_at,
                       metadata, project_id
                FROM sources
                WHERE source_id = %s
                """,
                (source_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return Source(
            source_id=row[0],
            source_type=row[1],
            uri=row[2],
            content_hash=row[3],
            collected_at=row[4],
            modified_at=row[5],
            metadata=row[6],
            project_id=row[7],
        )

    async def list_documents(
        self,
        project_id: str | None = None,
        source_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Document]:
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT d.document_id, d.elements, d.metadata,
                       s.source_id, s.source_type, s.uri, s.project_id
                FROM documents d
                JOIN sources s ON s.source_id = d.source_id
                WHERE (%s::text IS NULL OR d.project_id = %s)
                  AND (%s::text IS NULL OR d.source_id = %s)
                ORDER BY d.document_id
                LIMIT %s OFFSET %s
                """,
                (project_id, project_id, source_id, source_id, limit, offset),
            )
            rows = await cur.fetchall()
        return [
            Document(
                document_id=row[0],
                elements=row[1],
                metadata=row[2],
                source=SourceRef(source_id=row[3], source_type=row[4], uri=row[5], project_id=row[6]),
            )
            for row in rows
        ]

    async def get_document(self, document_id: str) -> Document | None:
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT d.document_id, d.elements, d.metadata,
                       s.source_id, s.source_type, s.uri, s.project_id
                FROM documents d
                JOIN sources s ON s.source_id = d.source_id
                WHERE d.document_id = %s
                """,
                (document_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return Document(
            document_id=row[0],
            elements=row[1],
            metadata=row[2],
            source=SourceRef(source_id=row[3], source_type=row[4], uri=row[5], project_id=row[6]),
        )

    async def save_chunks(self, chunks: list[Chunk]) -> None:
        rows: list[tuple[Any, ...]] = [
            (
                chunk.chunk_id,
                chunk.document_id,
                chunk.source_id,
                chunk.hierarchy_level.value,
                chunk.sequence_index,
                chunk.parent_chunk_id,
                chunk.text,
                chunk.token_count,
                Jsonb(chunk.element_ids),
                chunk.page_range[0] if chunk.page_range else None,
                chunk.page_range[1] if chunk.page_range else None,
            )
            for chunk in chunks
        ]
        if not rows:
            return
        async with self.conn.cursor() as cur:
            await cur.executemany(
                """
                INSERT INTO chunks (chunk_id, document_id, source_id, hierarchy_level,
                                     sequence_index, parent_chunk_id, text, token_count,
                                     element_ids, page_start, page_end)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (chunk_id) DO UPDATE SET
                    document_id = EXCLUDED.document_id,
                    source_id = EXCLUDED.source_id,
                    hierarchy_level = EXCLUDED.hierarchy_level,
                    sequence_index = EXCLUDED.sequence_index,
                    parent_chunk_id = EXCLUDED.parent_chunk_id,
                    text = EXCLUDED.text,
                    token_count = EXCLUDED.token_count,
                    element_ids = EXCLUDED.element_ids,
                    page_start = EXCLUDED.page_start,
                    page_end = EXCLUDED.page_end
                """,
                rows,
            )

    async def save_knowledge_objects(self, objects: list[KnowledgeObject]) -> None:
        rows: list[tuple[Any, ...]] = [
            (
                ko.knowledge_object_id,
                ko.type.value,
                ko.source.source_id,
                ko.document.document_id,
                Jsonb([e.model_dump(mode="json") for e in ko.evidence]),
                ko.summary,
                Jsonb(ko.content),
                Jsonb(ko.topics),
                ko.document_type,
                ko.extracted_at,
                ko.model_used,
                ko.version,
                ko.pipeline_version,
                ko.source_hash,
                ko.supersedes,
            )
            for ko in objects
        ]
        if not rows:
            return
        async with self.conn.cursor() as cur:
            await cur.executemany(
                """
                INSERT INTO knowledge_objects (knowledge_object_id, type, source_id, document_id,
                                                evidence, summary, content, topics, document_type,
                                                extracted_at, model_used, version, pipeline_version,
                                                source_hash, supersedes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (knowledge_object_id) DO UPDATE SET
                    type = EXCLUDED.type,
                    source_id = EXCLUDED.source_id,
                    document_id = EXCLUDED.document_id,
                    evidence = EXCLUDED.evidence,
                    summary = EXCLUDED.summary,
                    content = EXCLUDED.content,
                    topics = EXCLUDED.topics,
                    document_type = EXCLUDED.document_type,
                    extracted_at = EXCLUDED.extracted_at,
                    model_used = EXCLUDED.model_used,
                    version = EXCLUDED.version,
                    pipeline_version = EXCLUDED.pipeline_version,
                    source_hash = EXCLUDED.source_hash,
                    supersedes = EXCLUDED.supersedes
                """,
                rows,
            )

    async def list_knowledge_objects_by_source(self, source_id: str) -> list[KnowledgeObject]:
        """Every KnowledgeObject previously extracted from this `source_id`
        (across every prior document_id/content revision) - the candidate
        pool extraction/versioning.py matches a re-extraction's new objects
        against to auto-fill `supersedes`. Joins `sources` for
        `source_type`/`uri` since `knowledge_objects` only stores the id
        (see models/knowledge_object.py's DocumentRef/SourceRef note on why
        those stay lightweight refs)."""
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT ko.knowledge_object_id, ko.type, ko.document_id, ko.evidence, ko.summary,
                       ko.content, ko.topics, ko.document_type, ko.extracted_at, ko.model_used,
                       ko.version, ko.pipeline_version, ko.source_hash, ko.supersedes,
                       s.source_type, s.uri, s.project_id
                FROM knowledge_objects ko
                JOIN sources s ON s.source_id = ko.source_id
                WHERE ko.source_id = %s
                """,
                (source_id,),
            )
            rows = await cur.fetchall()

        return [
            KnowledgeObject(
                knowledge_object_id=row[0],
                type=row[1],
                source=SourceRef(source_id=source_id, source_type=row[14], uri=row[15], project_id=row[16]),
                document=DocumentRef(document_id=row[2]),
                evidence=[Evidence(**e) for e in row[3]],
                summary=row[4],
                content=row[5],
                topics=row[6],
                document_type=row[7],
                extracted_at=row[8],
                model_used=row[9],
                version=row[10],
                pipeline_version=row[11],
                source_hash=row[12],
                supersedes=row[13],
            )
            for row in rows
        ]

    async def list_knowledge_objects(
        self, source_id: str | None = None, type: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[KnowledgeObject]:
        """General-purpose paginated listing for the read API (wiki/api.md),
        as opposed to `list_knowledge_objects_by_source` above which exists
        specifically for supersedes-matching. `type`/`source_id` are
        optional filters (%s::text IS NULL trick short-circuits an unset
        filter without a second SQL string)."""
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT ko.knowledge_object_id, ko.type, ko.document_id, ko.evidence, ko.summary,
                       ko.content, ko.topics, ko.document_type, ko.extracted_at, ko.model_used,
                       ko.version, ko.pipeline_version, ko.source_hash, ko.supersedes,
                       s.source_id, s.source_type, s.uri, s.project_id
                FROM knowledge_objects ko
                JOIN sources s ON s.source_id = ko.source_id
                WHERE (%s::text IS NULL OR ko.source_id = %s)
                  AND (%s::text IS NULL OR ko.type = %s)
                ORDER BY ko.extracted_at DESC
                LIMIT %s OFFSET %s
                """,
                (source_id, source_id, type, type, limit, offset),
            )
            rows = await cur.fetchall()

        return [
            KnowledgeObject(
                knowledge_object_id=row[0],
                type=row[1],
                source=SourceRef(source_id=row[14], source_type=row[15], uri=row[16], project_id=row[17]),
                document=DocumentRef(document_id=row[2]),
                evidence=[Evidence(**e) for e in row[3]],
                summary=row[4],
                content=row[5],
                topics=row[6],
                document_type=row[7],
                extracted_at=row[8],
                model_used=row[9],
                version=row[10],
                pipeline_version=row[11],
                source_hash=row[12],
                supersedes=row[13],
            )
            for row in rows
        ]

    async def get_knowledge_object(self, knowledge_object_id: str) -> KnowledgeObject | None:
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT ko.knowledge_object_id, ko.type, ko.document_id, ko.evidence, ko.summary,
                       ko.content, ko.topics, ko.document_type, ko.extracted_at, ko.model_used,
                       ko.version, ko.pipeline_version, ko.source_hash, ko.supersedes,
                       s.source_id, s.source_type, s.uri, s.project_id
                FROM knowledge_objects ko
                JOIN sources s ON s.source_id = ko.source_id
                WHERE ko.knowledge_object_id = %s
                """,
                (knowledge_object_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return KnowledgeObject(
            knowledge_object_id=row[0],
            type=row[1],
            source=SourceRef(source_id=row[14], source_type=row[15], uri=row[16], project_id=row[17]),
            document=DocumentRef(document_id=row[2]),
            evidence=[Evidence(**e) for e in row[3]],
            summary=row[4],
            content=row[5],
            topics=row[6],
            document_type=row[7],
            extracted_at=row[8],
            model_used=row[9],
            version=row[10],
            pipeline_version=row[11],
            source_hash=row[12],
            supersedes=row[13],
        )

    async def list_relations(
        self,
        subject_id: str | None = None,
        object_id: str | None = None,
        type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Relation]:
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT relation_id, type, subject_id, object_id, label, confidence, evidence, created_at
                FROM relations
                WHERE (%s::text IS NULL OR subject_id = %s)
                  AND (%s::text IS NULL OR object_id = %s)
                  AND (%s::text IS NULL OR type = %s)
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                (subject_id, subject_id, object_id, object_id, type, type, limit, offset),
            )
            rows = await cur.fetchall()
        return [
            Relation(
                relation_id=row[0],
                type=row[1],
                subject_id=row[2],
                object_id=row[3],
                label=row[4],
                confidence=row[5],
                evidence=row[6],
                created_at=row[7],
            )
            for row in rows
        ]

    async def save_relations(self, relations: list[Relation]) -> None:
        rows: list[tuple[Any, ...]] = [
            (
                rel.relation_id,
                rel.type.value,
                rel.subject_id,
                rel.object_id,
                rel.label,
                rel.confidence,
                Jsonb(rel.evidence),
                rel.created_at,
            )
            for rel in relations
        ]
        if not rows:
            return
        async with self.conn.cursor() as cur:
            await cur.executemany(
                """
                INSERT INTO relations (relation_id, type, subject_id, object_id, label,
                                        confidence, evidence, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (relation_id) DO UPDATE SET
                    type = EXCLUDED.type,
                    subject_id = EXCLUDED.subject_id,
                    object_id = EXCLUDED.object_id,
                    label = EXCLUDED.label,
                    confidence = EXCLUDED.confidence,
                    evidence = EXCLUDED.evidence,
                    created_at = EXCLUDED.created_at
                """,
                rows,
            )

    async def get_sync_cursor(self, source: str) -> dict[str, Any] | None:
        """Returns the persisted `Cursor.data` for `source` (e.g. "slack"),
        or None when this source has never completed an incremental-sync run
        (ingest.py's run_saas_ingest then does a full initial_sync, same as
        before this table existed)."""
        async with self.conn.cursor() as cur:
            await cur.execute("SELECT cursor_data FROM sync_cursors WHERE source = %s", (source,))
            row = await cur.fetchone()
        return row[0] if row is not None else None

    async def save_sync_cursor(self, source: str, cursor_data: dict[str, Any], updated_at: datetime) -> None:
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO sync_cursors (source, cursor_data, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (source) DO UPDATE SET
                    cursor_data = EXCLUDED.cursor_data,
                    updated_at = EXCLUDED.updated_at
                """,
                (source, Jsonb(cursor_data), updated_at),
            )

    async def save_embeddings(self, records: list["EmbeddingRecord"]) -> None:
        now = datetime.now(timezone.utc)
        rows: list[tuple[Any, ...]] = [
            (
                rec.knowledge_object_id,
                rec.embedding_model,
                # register_vector_async() (connect(), above) only registers a
                # dumper for pgvector.Vector/numpy.ndarray, not a plain
                # list[float] - passed as-is, psycopg adapts it as a
                # double precision[] array instead of `vector`, and this
                # INSERT fails with "column is of type vector but expression
                # is of type double precision[]".
                Vector(rec.embedding),
                rec.embedded_text,
                rec.embedded_text_hash,
                rec.project_id,
                now,
            )
            for rec in records
        ]
        if not rows:
            return
        async with self.conn.cursor() as cur:
            await cur.executemany(
                """
                INSERT INTO knowledge_object_embeddings (knowledge_object_id, embedding_model,
                                                           embedding, embedded_text, embedded_text_hash,
                                                           project_id, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (knowledge_object_id, embedding_model) DO UPDATE SET
                    embedding = EXCLUDED.embedding,
                    embedded_text = EXCLUDED.embedded_text,
                    embedded_text_hash = EXCLUDED.embedded_text_hash,
                    project_id = EXCLUDED.project_id,
                    created_at = EXCLUDED.created_at
                """,
                rows,
            )

    async def list_knowledge_objects_missing_embedding(
        self, embedding_model: str, project_id: str | None, limit: int
    ) -> list[KnowledgeObject]:
        """KnowledgeObjects that have no row yet in `knowledge_object_embeddings`
        for `embedding_model` - the backfill CLI command's work queue. Ordered
        by `extracted_at` so a resumed/repeated backfill run makes steady
        forward progress instead of re-scanning the same head of the table."""
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT ko.knowledge_object_id, ko.type, ko.document_id, ko.evidence, ko.summary,
                       ko.content, ko.topics, ko.document_type, ko.extracted_at, ko.model_used,
                       ko.version, ko.pipeline_version, ko.source_hash, ko.supersedes,
                       s.source_id, s.source_type, s.uri, s.project_id
                FROM knowledge_objects ko
                JOIN sources s ON s.source_id = ko.source_id
                LEFT JOIN knowledge_object_embeddings e
                  ON e.knowledge_object_id = ko.knowledge_object_id AND e.embedding_model = %s
                WHERE e.knowledge_object_id IS NULL
                  AND (%s::text IS NULL OR s.project_id = %s)
                ORDER BY ko.extracted_at
                LIMIT %s
                """,
                (embedding_model, project_id, project_id, limit),
            )
            rows = await cur.fetchall()

        return [
            KnowledgeObject(
                knowledge_object_id=row[0],
                type=row[1],
                source=SourceRef(source_id=row[14], source_type=row[15], uri=row[16], project_id=row[17]),
                document=DocumentRef(document_id=row[2]),
                evidence=[Evidence(**e) for e in row[3]],
                summary=row[4],
                content=row[5],
                topics=row[6],
                document_type=row[7],
                extracted_at=row[8],
                model_used=row[9],
                version=row[10],
                pipeline_version=row[11],
                source_hash=row[12],
                supersedes=row[13],
            )
            for row in rows
        ]

    async def search_knowledge_objects_by_embedding(
        self,
        embedding: list[float],
        embedding_model: str,
        project_id: str | None,
        type: str | None,
        limit: int,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Nearest-neighbor search over `knowledge_object_embeddings`' HNSW
        index (wiki/agent-search.md's vector_search) - `ORDER BY embedding
        <=> %s` (pgvector's cosine-distance operator, matching the index's
        `vector_cosine_ops`) `LIMIT %s`. `embedding` arrives as a plain
        `list[float]` but is wrapped in `pgvector.Vector` before being bound
        as a query parameter - `register_vector_async()` (`connect()`) only
        registers a dumper for `Vector`/`numpy.ndarray`, not a bare `list`,
        so an unwrapped list gets sent as a `double precision[]` array and
        `<=>` against `vector` fails with "operator does not exist" (the
        same reason `save_embeddings` wraps it on the INSERT side).

        `since`/`until` (search/temporal.py, "⑥ Temporal Search") filter on
        `ko.extracted_at` - `until` is exclusive, matching
        `search/temporal.py::TimeRange`'s half-open interval contract.

        Returns dict rows, not `KnowledgeObject` - a search hit only needs
        enough to decide relevance (type/summary/topics) plus a similarity
        `score`, not the full envelope (evidence/content/version/...) every
        other `list_*`/`get_*` method here returns."""
        vector = Vector(embedding)
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT ko.knowledge_object_id, ko.type, ko.summary, ko.topics, ko.document_type,
                       s.project_id, e.embedding <=> %s AS distance
                FROM knowledge_object_embeddings e
                JOIN knowledge_objects ko ON ko.knowledge_object_id = e.knowledge_object_id
                JOIN sources s ON s.source_id = ko.source_id
                WHERE e.embedding_model = %s
                  AND (%s::text IS NULL OR e.project_id = %s)
                  AND (%s::text IS NULL OR ko.type = %s)
                  AND (%s::timestamptz IS NULL OR ko.extracted_at >= %s)
                  AND (%s::timestamptz IS NULL OR ko.extracted_at < %s)
                ORDER BY e.embedding <=> %s
                LIMIT %s
                """,
                (
                    vector,
                    embedding_model,
                    project_id,
                    project_id,
                    type,
                    type,
                    since,
                    since,
                    until,
                    until,
                    vector,
                    limit,
                ),
            )
            rows = await cur.fetchall()

        return [
            {
                "knowledge_object_id": row[0],
                "type": row[1],
                "summary": row[2],
                "topics": row[3],
                "document_type": row[4],
                "project_id": row[5],
                # Cosine distance is in [0, 2] - 1 - distance is the matching
                # cosine similarity (1.0 = identical direction), easier for a
                # caller/model to read as "higher is more relevant".
                "score": 1.0 - float(row[6]),
            }
            for row in rows
        ]

    async def execute_readonly_query(self, sql: str, limit: int) -> tuple[list[tuple[Any, ...]], list[str]]:
        """Runs an already-validated, single read-only SELECT/WITH statement
        (`search/sql.py`'s Text-to-SQL tool - `sql` has already passed that
        module's string-level checks by the time it reaches here) with two
        more independent layers of defense against it turning out to be a
        write after all:

        1. `SET TRANSACTION READ ONLY` - Postgres itself rejects any write
           statement inside this transaction, regardless of what the
           string-level check upstream missed.
        2. `force_rollback=True` - the transaction is rolled back at the end
           of the block unconditionally, even on success, so nothing this
           query does can persist even in some edge case neither (1) nor the
           upstream check anticipated.

        `limit` is enforced by wrapping the caller's query as a subquery
        (`SELECT * FROM (sql) AS sql_search LIMIT <limit>`) rather than
        trusting whatever `LIMIT` (if any) `sql` itself contains - a result
        set is capped no matter what the caller wrote or forgot to write.
        `limit` is interpolated as a literal `int(limit)` (never passed as a
        `%s` query parameter alongside `sql`) - `sql` is untrusted, arbitrary
        text that legitimately contains literal `%` (e.g. `ILIKE '%김%'`),
        and psycopg's client-side placeholder scanner runs over the *whole*
        query string whenever a `params` tuple is given, so a stray `%` in
        `sql` gets misparsed as a broken placeholder. `int()` makes the
        literal-interpolation safe (whatever `limit` was, only its digits
        ever reach the query)."""
        wrapped = f"SELECT * FROM (\n{sql}\n) AS sql_search LIMIT {int(limit)}"
        async with self.conn.transaction(force_rollback=True):
            await self.conn.execute("SET TRANSACTION READ ONLY")
            async with self.conn.cursor() as cur:
                await cur.execute(wrapped)
                rows = await cur.fetchall()
                columns = [d.name for d in cur.description] if cur.description else []
        return rows, columns
