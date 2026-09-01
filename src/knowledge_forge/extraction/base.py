from abc import ABC, abstractmethod

from knowledge_forge.models.chunk import Chunk
from knowledge_forge.models.document import Document
from knowledge_forge.models.knowledge_object import KnowledgeObject
from knowledge_forge.models.source import Source


class Extractor(ABC):
    """Runs Knowledge Extraction over a single Chunk, producing a KnowledgeObject."""

    @abstractmethod
    async def extract(self, chunk: Chunk, document: Document, source: Source) -> KnowledgeObject:
        ...
