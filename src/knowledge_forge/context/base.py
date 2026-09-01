"""Context Reconstruction interface.

Inserted between a Collector and the Parser stage: a Collector's natural
yield granularity (one Slack message, one Gmail message) is not always the
right *semantic* unit for Knowledge Extraction - see wiki/roadmap.md's
Source-aware Context Reconstruction section for the motivating example. A
`ContextReconstructor` re-groups/merges a stream of `RawItem`s from one
source into RawItems that represent complete semantic units before they
reach a Parser.
"""

from abc import ABC, abstractmethod
from typing import Iterator

from knowledge_forge.collectors.base import RawItem


class ContextReconstructor(ABC):
    @abstractmethod
    def reconstruct(self, items: Iterator[RawItem]) -> Iterator[RawItem]:
        """Transform a raw item stream into semantically complete units."""


class NoopReconstructor(ContextReconstructor):
    """Identity pass-through - each RawItem is already its own semantic unit
    (File, Notion, Google Drive: see roadmap.md)."""

    def reconstruct(self, items: Iterator[RawItem]) -> Iterator[RawItem]:
        yield from items
