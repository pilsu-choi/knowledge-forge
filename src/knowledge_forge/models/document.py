from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

from knowledge_forge.models.source import SourceRef


class ElementBase(BaseModel):
    element_id: str
    page: int | None = None


class Heading(ElementBase):
    type: Literal["heading"] = "heading"
    level: int = Field(ge=1, le=6)
    text: str


class TextBlock(ElementBase):
    type: Literal["text"] = "text"
    text: str


class TableElement(ElementBase):
    type: Literal["table"] = "table"
    rows: list[list[str]]
    caption: str | None = None


class ListElement(ElementBase):
    type: Literal["list"] = "list"
    items: list[str]
    ordered: bool = False


class ImageElement(ElementBase):
    type: Literal["image"] = "image"
    uri: str | None = None
    alt_text: str | None = None
    ocr_text: str | None = None


DocumentElement = Annotated[
    Union[Heading, TextBlock, TableElement, ListElement, ImageElement],
    Field(discriminator="type"),
]


class DocumentMetadata(BaseModel):
    title: str | None = None
    author: str | None = None
    language: str | None = None
    page_count: int | None = None
    created_at: datetime | None = None


class Document(BaseModel):
    """The common structural representation every Parser produces."""

    document_id: str
    source: SourceRef
    elements: list[DocumentElement]
    metadata: DocumentMetadata = Field(default_factory=DocumentMetadata)
