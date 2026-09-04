from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

EmbeddingTargetKind = Literal["knowledge_object", "chunk"]


@dataclass
class EmbeddingRecord:
    """Corresponds to one row of the `knowledge_object_embeddings` table -
    `PostgresStore` upserts this shape as-is (see storage/postgres_store.py's
    `save_embeddings()` and storage/store.py's `record_embeddings()` /
    `record_chunk_embeddings()`, which expect exactly these field names).
    Do not rename these fields.

    Exactly one of `knowledge_object_id` / `chunk_id` is set, matching
    `target_kind` (the table's CHECK constraint enforces the same XOR).
    `document_id` is always set - it is the identifier both grains share,
    see schema.sql's comment on the table."""

    target_kind: EmbeddingTargetKind
    document_id: str
    embedding_model: str
    embedding: list[float]
    embedded_text: str
    embedded_text_hash: str
    project_id: str
    knowledge_object_id: str | None = None
    chunk_id: str | None = None

    def __post_init__(self) -> None:
        if self.target_kind == "knowledge_object":
            if not self.knowledge_object_id or self.chunk_id is not None:
                raise ValueError("target_kind='knowledge_object' requires knowledge_object_id and no chunk_id")
        elif self.target_kind == "chunk":
            if not self.chunk_id or self.knowledge_object_id is not None:
                raise ValueError("target_kind='chunk' requires chunk_id and no knowledge_object_id")
        else:
            raise ValueError(f"unknown target_kind: {self.target_kind!r}")

    @property
    def target_id(self) -> str:
        """The embedded object's own id (`knowledge_object_id` or `chunk_id`,
        whichever `target_kind` names)."""
        target = self.knowledge_object_id if self.target_kind == "knowledge_object" else self.chunk_id
        assert target is not None  # guaranteed by __post_init__
        return target

    @property
    def embedding_id(self) -> str:
        """Deterministic surrogate PK for the `knowledge_object_embeddings`
        row - `stable_id(target_kind, target_id, embedding_model)`, so
        re-embedding the same target with the same model upserts in place
        (the old composite-PK behaviour) rather than inserting a duplicate."""
        from knowledge_forge.ids import stable_id

        return stable_id("embedding", self.target_kind, self.target_id, self.embedding_model)


class EmbeddingProvider(ABC):
    """Turns a batch of texts (KnowledgeObject summaries or raw Chunk text -
    see embedding/text.py) into vectors. `embed()` takes a list rather than
    one text at a time because embedding providers price and rate-limit per
    request, not per text - batching many texts into one call (as
    pipeline.py does for a document's merged KnowledgeObjects, or its
    chunks) is both cheaper and faster than one call per object, mirroring
    why Extractor.extract() takes a bundle of Chunks instead of a single
    Chunk.
    """

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Returns one embedding vector per input text, in the same order as
        `texts` (same length too - every input gets a vector back)."""
        ...
