from abc import ABC, abstractmethod

from knowledge_forge.models.chunk import Chunk
from knowledge_forge.models.document import Document


class Chunker(ABC):
    """Splits a Document into a list of provenance-linked Chunks."""

    @abstractmethod
    def chunk(self, document: Document) -> list[Chunk]:
        ...
