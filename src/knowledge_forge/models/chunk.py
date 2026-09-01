from enum import Enum

from pydantic import BaseModel, Field


class HierarchyLevel(str, Enum):
    """Document -> Chapter -> Section -> Paragraph -> Sentence -> Line.

    A chunk is only pushed to a finer level than DOCUMENT when the coarser
    unit's text exceeds the configured token budget - see chunking.adaptive.
    """

    DOCUMENT = "document"
    CHAPTER = "chapter"
    SECTION = "section"
    PARAGRAPH = "paragraph"
    SENTENCE = "sentence"
    LINE = "line"


class Chunk(BaseModel):
    chunk_id: str
    document_id: str
    source_id: str
    hierarchy_level: HierarchyLevel
    sequence_index: int
    parent_chunk_id: str | None = None
    text: str
    token_count: int
    element_ids: list[str] = Field(default_factory=list)
    page_range: tuple[int, int] | None = None
