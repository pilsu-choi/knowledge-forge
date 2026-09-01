"""PipelineStore: the single persistence facade pipeline.py depends on.

Bundles the disk-based raw blob store and the PostgreSQL structured store
so callers (cli.py, scheduling) construct one object instead of wiring two.
"""

from pathlib import Path

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.models.chunk import Chunk
from knowledge_forge.models.document import Document
from knowledge_forge.models.knowledge_object import KnowledgeObject
from knowledge_forge.models.source import Source
from knowledge_forge.storage.postgres_store import PostgresStore
from knowledge_forge.storage.raw_store import RawBlobStore


class PipelineStore:
    def __init__(self, database_url: str, data_dir: Path | str):
        self._db = PostgresStore(database_url)
        self._raw = RawBlobStore(data_dir)

    def __enter__(self) -> "PipelineStore":
        self._db.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._db.close()

    def init_schema(self) -> None:
        self._db.init_schema()

    def record_source(self, source: Source, item: RawItem) -> None:
        raw_path = self._raw.save(item)
        self._db.save_source(source, str(raw_path))

    def record_document(self, document: Document) -> None:
        self._db.save_document(document)

    def record_chunks(self, chunks: list[Chunk]) -> None:
        self._db.save_chunks(chunks)

    def record_knowledge_object(self, ko: KnowledgeObject) -> None:
        self._db.save_knowledge_object(ko)
