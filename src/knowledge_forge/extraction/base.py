from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from knowledge_forge.models.chunk import Chunk
from knowledge_forge.models.document import Document
from knowledge_forge.models.knowledge_object import KnowledgeObject
from knowledge_forge.models.relation import Relation
from knowledge_forge.models.source import Source


@dataclass
class ExtractionOutcome:
    """A chunk extracts into zero or more KnowledgeObjects plus the graph
    Relations connecting them (and connecting them to entities) - not a
    single KnowledgeObject. See models/knowledge_object.py."""

    objects: list[KnowledgeObject] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)


class Extractor(ABC):
    """Runs Knowledge Extraction over a single Chunk, producing the
    KnowledgeObjects and Relations grounded in that chunk's text."""

    @abstractmethod
    async def extract(self, chunk: Chunk, document: Document, source: Source) -> ExtractionOutcome:
        ...
