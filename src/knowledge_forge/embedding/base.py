from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class EmbeddingRecord:
    """Corresponds to one row of the `knowledge_object_embeddings` table -
    `PostgresStore` upserts this shape as-is (see storage/postgres_store.py's
    `save_embeddings()` and storage/store.py's `record_embeddings()`, which
    expect exactly these field names/order). Do not rename these fields."""

    knowledge_object_id: str
    embedding_model: str
    embedding: list[float]
    embedded_text: str
    embedded_text_hash: str
    project_id: str


class EmbeddingProvider(ABC):
    """Turns a batch of KnowledgeObject texts into vectors. `embed()` takes a
    list rather than one text at a time because embedding providers price
    and rate-limit per request, not per text - batching many texts into one
    call (as pipeline.py does for a document's merged KnowledgeObjects) is
    both cheaper and faster than one call per object, mirroring why
    Extractor.extract() takes a bundle of Chunks instead of a single Chunk.
    """

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Returns one embedding vector per input text, in the same order as
        `texts` (same length too - every input gets a vector back)."""
        ...
