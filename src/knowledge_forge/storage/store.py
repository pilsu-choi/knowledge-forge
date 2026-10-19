"""PipelineStore: the single persistence facade pipeline.py depends on.

Bundles the disk-based raw blob store and the PostgreSQL structured store
so callers (cli.py, scheduling) construct one object instead of wiring two.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.models.chunk import Chunk, HierarchyLevel
from knowledge_forge.models.document import Document
from knowledge_forge.models.knowledge_object import KnowledgeObject
from knowledge_forge.models.link import DocumentLink
from knowledge_forge.models.relation import Relation
from knowledge_forge.models.source import Source
from knowledge_forge.storage.postgres_store import PostgresStore
from knowledge_forge.storage.raw_store import RawBlobStore

if TYPE_CHECKING:
    from knowledge_forge.embedding.base import EmbeddingProvider
    from knowledge_forge.entity_resolution import EntityResolution


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

    async def get_source_content_hash(self, source_id: str) -> str | None:
        """`run_pipeline`'s unchanged-source check - see
        `PostgresStore.get_source_content_hash`."""
        return await self._db.get_source_content_hash(source_id)

    async def record_chunks(self, document_id: str, chunks: list[Chunk]) -> None:
        """`chunks` replaces `document_id`'s whole chunk set - see `PostgresStore.save_chunks`."""
        await self._db.save_chunks(chunks, document_id=document_id)

    async def record_knowledge_objects(self, objects: list[KnowledgeObject]) -> None:
        await self._db.save_knowledge_objects(objects)

    async def record_relations(self, relations: list[Relation]) -> None:
        await self._db.save_relations(relations)

    async def record_entities(self, resolution: "EntityResolution") -> None:
        """Resolved entities + mentions (entity_resolution.py) - after
        `record_knowledge_objects`, which the mentions' FK needs."""
        await self._db.save_entities(resolution.entities, resolution.mentions)

    async def list_prior_knowledge_objects(self, source_id: str) -> list[KnowledgeObject]:
        """The current "head" (not-yet-superseded) KnowledgeObjects for this
        source - see PostgresStore.list_knowledge_objects_by_source for why
        it's restricted to those rather than every object ever extracted
        from this source."""
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
                target_kind="knowledge_object",
                knowledge_object_id=ko.knowledge_object_id,
                document_id=ko.document.document_id,
                embedding_model=embedding_model,
                embedding=vector,
                embedded_text=text,
                embedded_text_hash=content_hash(text.encode("utf-8")),
                project_id=ko.source.project_id,
            )
            for ko, text, vector in zip(objects, texts, vectors, strict=True)
        ]
        await self._db.save_embeddings(records)

    async def record_chunk_embeddings(
        self,
        chunks: list[Chunk],
        project_id: str,
        embedder: "EmbeddingProvider",
        embedding_model: str,
    ) -> None:
        """Chunk-grain counterpart of `record_embeddings`
        (`KF_EMBEDDING_SOURCE=chunk`, wiki/embeddings.md): embeds each
        chunk's raw text and stores a `target_kind='chunk'` row. Shared by
        pipeline.py (right after `record_chunks`, before extraction even
        starts) and the `embed --embedding-source chunk` backfill. All
        `chunks` must belong to `project_id` - a `Chunk` doesn't carry it,
        so the caller (which has the Source, or the join in
        `list_chunks_missing_embedding`) supplies it."""
        if not chunks:
            return
        from knowledge_forge.embedding.base import EmbeddingRecord
        from knowledge_forge.embedding.text import build_chunk_embedding_text
        from knowledge_forge.ids import content_hash

        texts = [build_chunk_embedding_text(chunk) for chunk in chunks]
        vectors = await embedder.embed(texts)
        records = [
            EmbeddingRecord(
                target_kind="chunk",
                chunk_id=chunk.chunk_id,
                document_id=chunk.document_id,
                embedding_model=embedding_model,
                embedding=vector,
                embedded_text=text,
                embedded_text_hash=content_hash(text.encode("utf-8")),
                project_id=project_id,
            )
            for chunk, text, vector in zip(chunks, texts, vectors, strict=True)
        ]
        await self._db.save_embeddings(records)

    async def list_knowledge_objects_missing_embedding(
        self, embedding_model: str, project_id: str | None = None, limit: int = 50
    ) -> list[KnowledgeObject]:
        return await self._db.list_knowledge_objects_missing_embedding(embedding_model, project_id, limit)

    async def list_chunks_missing_embedding(
        self, embedding_model: str, project_id: str | None = None, limit: int = 50
    ) -> list[tuple[Chunk, str]]:
        """`(chunk, project_id)` pairs - see `PostgresStore.list_chunks_missing_embedding`."""
        return await self._db.list_chunks_missing_embedding(embedding_model, project_id, limit)

    async def search_by_embedding(
        self,
        embedding: list[float],
        embedding_model: str,
        project_id: str | None = None,
        type: str | None = None,
        limit: int = 10,
        since: datetime | None = None,
        until: datetime | None = None,
        target_kind: str | None = None,
        document_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """`search/service.py::vector_search`'s single entry point into
        storage - mirrors `record_embeddings`' role of being the one place
        the caller needs, rather than reaching into `PostgresStore` directly.
        `target_kind` (None = both grains) narrows to 'knowledge_object' or
        'chunk' rows and `document_ids` to those documents, see
        `PostgresStore.search_by_embedding`."""
        return await self._db.search_by_embedding(
            embedding, embedding_model, project_id, type, limit, since, until, target_kind, document_ids
        )

    async def embedding_coverage(self, embedding_model: str, project_id: str | None = None) -> dict[str, Any]:
        """임베딩 적재 현황 진단 - 벡터 검색 0히트의 세 가지 원인(행 없음 / 모델 이름 불일치 /
        다른 프로젝트)을 한 번에 보여준다. `PostgresStore.embedding_coverage` 참고."""
        return await self._db.embedding_coverage(embedding_model, project_id)

    async def get_document(self, document_id: str) -> Document | None:
        return await self._db.get_document(document_id)

    async def get_source(self, source_id: str) -> Source | None:
        return await self._db.get_source(source_id)

    async def list_chunks_by_document(
        self, document_id: str, *, hierarchy_level: HierarchyLevel | None = None
    ) -> list[Chunk]:
        """`search/document.py::read_document`'s entry point into storage
        (with `get_document`/`get_source` above) - reading order, every
        hierarchy level unless narrowed, see
        `PostgresStore.list_chunks_by_document` for why."""
        return await self._db.list_chunks_by_document(document_id, hierarchy_level=hierarchy_level)

    async def execute_readonly_query(self, sql: str, limit: int) -> tuple[list[tuple[Any, ...]], list[str]]:
        """`search/sql.py::sql_search`'s single entry point into storage."""
        return await self._db.execute_readonly_query(sql, limit)

    async def search_documents_by_title(
        self,
        query: str,
        *,
        project_id: str | None = None,
        source_type: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """`search/service.py::document_search`'s entry point - see
        `PostgresStore.search_documents_by_title`."""
        return await self._db.search_documents_by_title(
            query, project_id=project_id, source_type=source_type, since=since, until=until, limit=limit
        )

    async def search_chunks_lexical(
        self,
        query: str,
        *,
        project_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """`search/service.py::lexical_search`'s entry point - see
        `PostgresStore.search_chunks_lexical`."""
        return await self._db.search_chunks_lexical(query, project_id=project_id, since=since, until=until, limit=limit)

    async def backfill_document_titles(self, project_id: str | None = None, *, overwrite: bool = False) -> int:
        """`knowledge-forge db backfill-titles` - see `PostgresStore.backfill_document_titles`."""
        return await self._db.backfill_document_titles(project_id, overwrite=overwrite)

    async def backfill_slack_display_names(
        self, directory, project_id: str | None = None, refresh_first_message: bool = False
    ) -> dict:
        """`knowledge-forge db backfill-slack-names` - see `PostgresStore.backfill_slack_display_names`."""
        return await self._db.backfill_slack_display_names(directory, project_id, refresh_first_message)

    @property
    def conn(self):
        """CLI의 `--dry-run`이 백필을 트랜잭션으로 감싸 롤백하려면 같은 커넥션이 필요하다."""
        return self._db.conn

    async def normalize_unicode_text(self, project_id: str | None = None) -> tuple[int, int]:
        """`knowledge-forge db normalize-unicode` - see `PostgresStore.normalize_unicode_text`."""
        return await self._db.normalize_unicode_text(project_id)

    async def link_document(self, source: Source, document: Document) -> list[DocumentLink]:
        """Version-family relink + source-metadata links for one just-stored
        Document (document_links.link_document) - called by pipeline.py right
        after `record_document`, which must come first so the family query
        sees the new row."""
        from knowledge_forge.document_links import link_document

        return await link_document(self._db, source, document)

    async def record_document_links(self, links: list[DocumentLink]) -> None:
        await self._db.save_document_links(links)

    async def list_document_links(
        self, document_id: str, *, direction: str = "both", link_type: str | None = None
    ) -> list[DocumentLink]:
        return await self._db.list_document_links(document_id, direction=direction, link_type=link_type)

    async def list_version_family(self, family_key: str, project_id: str) -> list[Document]:
        """Oldest -> newest; see `PostgresStore.list_version_family`."""
        return await self._db.list_version_family(family_key, project_id)

    async def backfill_document_versions_and_links(self, project_id: str | None = None) -> dict[str, Any]:
        """`knowledge-forge db backfill-links` - see document_links.py."""
        from knowledge_forge.document_links import backfill_document_versions_and_links

        return await backfill_document_versions_and_links(self._db, project_id)

    async def get_sync_cursor(self, source: str) -> dict[str, Any] | None:
        return await self._db.get_sync_cursor(source)

    async def save_sync_cursor(self, source: str, cursor_data: dict[str, Any]) -> None:
        await self._db.save_sync_cursor(source, cursor_data, datetime.now(timezone.utc))
