"""PostgreSQL persistence for Source/Document/Chunk/KnowledgeObject.

Every table's primary key is a deterministic stable_id (knowledge_forge.ids),
so every write here is an idempotent upsert - re-running the pipeline on
unchanged input is safe to repeat.
"""

from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from knowledge_forge.models.chunk import Chunk
from knowledge_forge.models.document import Document
from knowledge_forge.models.knowledge_object import DocumentRef, Evidence, KnowledgeObject
from knowledge_forge.models.relation import Relation
from knowledge_forge.models.source import Source, SourceRef

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


class PostgresStore:
    def __init__(self, dsn: str):
        self._dsn = dsn
        self._conn: psycopg.AsyncConnection | None = None

    async def connect(self) -> "PostgresStore":
        self._conn = await psycopg.AsyncConnection.connect(self._dsn, autocommit=True)
        return self

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
