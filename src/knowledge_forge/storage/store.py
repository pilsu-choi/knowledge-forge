"""PipelineStore: the single persistence facade pipeline.py depends on.

Bundles the disk-based raw blob store and the PostgreSQL structured store
so callers (cli.py, scheduling) construct one object instead of wiring two.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.models.chunk import Chunk
from knowledge_forge.models.document import Document
from knowledge_forge.models.knowledge_object import KnowledgeObject
from knowledge_forge.models.relation import Relation
from knowledge_forge.models.source import Source
from knowledge_forge.storage.postgres_store import PostgresStore
from knowledge_forge.storage.raw_store import RawBlobStore

if TYPE_CHECKING:
    from knowledge_forge.embedding.base import EmbeddingProvider


class PipelineStore:
    def __init__(self, database_url: str, data_dir: Path | str):
        self._db = PostgresStore(database_url)
        self._raw = RawBlobStore(data_dir)

    async def __aenter__(self) -> "PipelineStore":
        await self._db.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._db.close()

    async def init_schema(self) -> None:
        await self._db.init_schema()

    async def record_source(self, source: Source, item: RawItem) -> None:
        raw_path = self._raw.save(item, source.project_id)
        await self._db.save_source(source, str(raw_path))

    async def record_document(self, document: Document) -> None:
        await self._db.save_document(document)

    async def record_chunks(self, chunks: list[Chunk]) -> None:
        await self._db.save_chunks(chunks)

    async def record_knowledge_objects(self, objects: list[KnowledgeObject]) -> None:
        await self._db.save_knowledge_objects(objects)

    async def record_relations(self, relations: list[Relation]) -> None:
        await self._db.save_relations(relations)

    async def list_prior_knowledge_objects(self, source_id: str) -> list[KnowledgeObject]:
        return await self._db.list_knowledge_objects_by_source(source_id)

    async def record_embeddings(
        self,
        objects: list[KnowledgeObject],
        embedder: "EmbeddingProvider",
        embedding_model: str,
    ) -> None:
        """Single entry point shared by the real-time pipeline path
        (pipeline.py) and the backfill CLI command (cli.py's `embed` command)
        - the text-building/hashing logic lives here once instead of being
        duplicated in both callers."""
        if not objects:
            return
        from knowledge_forge.embedding.base import EmbeddingRecord
        from knowledge_forge.embedding.text import build_embedding_text
        from knowledge_forge.ids import content_hash

        texts = [build_embedding_text(ko) for ko in objects]
        vectors = await embedder.embed(texts)
        records = [
            EmbeddingRecord(
                knowledge_object_id=ko.knowledge_object_id,
                embedding_model=embedding_model,
                embedding=vector,
                embedded_text=text,
                embedded_text_hash=content_hash(text.encode("utf-8")),
                project_id=ko.source.project_id,
            )
            for ko, text, vector in zip(objects, texts, vectors)
        ]
        await self._db.save_embeddings(records)

    async def list_knowledge_objects_missing_embedding(
        self, embedding_model: str, project_id: str | None = None, limit: int = 50
    ) -> list[KnowledgeObject]:
        return await self._db.list_knowledge_objects_missing_embedding(embedding_model, project_id, limit)

    async def search_by_embedding(
        self,
        embedding: list[float],
        embedding_model: str,
        project_id: str | None = None,
        type: str | None = None,
        limit: int = 10,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """`search/service.py::vector_search`'s single entry point into
        storage - mirrors `record_embeddings`' role of being the one place
        the caller needs, rather than reaching into `PostgresStore` directly."""
        return await self._db.search_knowledge_objects_by_embedding(
            embedding, embedding_model, project_id, type, limit, since, until
        )

    async def execute_readonly_query(self, sql: str, limit: int) -> tuple[list[tuple[Any, ...]], list[str]]:
        """`search/sql.py::sql_search`'s single entry point into storage."""
        return await self._db.execute_readonly_query(sql, limit)

    async def get_sync_cursor(self, source: str) -> dict[str, Any] | None:
        return await self._db.get_sync_cursor(source)

    async def save_sync_cursor(self, source: str, cursor_data: dict[str, Any]) -> None:
        await self._db.save_sync_cursor(source, cursor_data, datetime.now(timezone.utc))
