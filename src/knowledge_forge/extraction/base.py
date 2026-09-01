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
    """Runs Knowledge Extraction over a bundle of one or more related Chunks
    (see extraction/grouping.py), producing the KnowledgeObjects and
    Relations grounded in that bundle's text. `chunks` is always
    non-empty; a bundle of one chunk is the common case and behaves exactly
    like the old one-chunk-per-call interface."""

    @abstractmethod
    async def extract(self, chunks: list[Chunk], document: Document, source: Source) -> ExtractionOutcome:
        ...
