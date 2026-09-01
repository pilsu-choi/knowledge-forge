from abc import ABC, abstractmethod
from typing import ClassVar

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.models.document import Document
from knowledge_forge.models.source import SourceRef


class Parser(ABC):
    """Converts a RawItem from any source into the common Document structure."""

    supported_extensions: ClassVar[set[str]]

    @abstractmethod
    def parse(self, item: RawItem, source_ref: SourceRef) -> Document:
        ...
