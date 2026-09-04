from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from knowledge_forge.models.source import SourceRef


class ElementBase(BaseModel):
    element_id: str
    page: int | None = None
    # (x0, top, x1, bottom) in the source page's coordinate space, when the
    # parser can supply one (currently: PDF only). Lets a Knowledge Object
    # cite not just "which page" but "where on the page" it came from.
    bbox: tuple[float, float, float, float] | None = None
    # 0-based nesting depth inferred from the source document's own
    # indentation (DOCX paragraph left-indent, PDF line x-position, HWPX
    # paragraph margin - see each parser). None when the parser found no
    # indent signal (or predates this field) rather than "not indented" -
    # callers should not treat None and 0 as the same thing.
    indent_level: int | None = None


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
    # "vlm" when a vision model transcribed this table from a rendered
    # crop (see vision.extractor); "rule_based" for a deterministic parser
    # (openpyxl, python-docx, ...); None when the parser predates this field.
    extraction_method: Literal["rule_based", "vlm"] | None = None
    # How many leading rows of `rows` are header rows (e.g. 2 for a
    # multi-level XLSX header where a merged top row like "2025년" sits
    # above "기본급"/"가산율"). Default 1 preserves the single-header-row
    # assumption every parser but xlsx still makes.
    header_rows: int = 1
    # Optional nested representation of a multi-level-header table (see
    # parsers/xlsx.py), keyed by the leaf header labels. `rows` stays the
    # flat, universally-consumed form (chunking renders it directly); this
    # is purely additive for future ontology/graph consumers - nothing in
    # chunking reads it today.
    structured: dict | None = None
    # Cell-ref (e.g. "B7") -> original formula string, for cells whose
    # value in `rows` is a computed/cached result rather than a literal.
    # Only populated where the source format preserves formulas (XLSX).
    formulas: dict[str, str] | None = None


class ListElement(ElementBase):
    type: Literal["list"] = "list"
    items: list[str]
    ordered: bool = False


class ImageElement(ElementBase):
    type: Literal["image"] = "image"
    uri: str | None = None
    alt_text: str | None = None
    # A vision model's transcription/description of the image content
    # (figure, chart, diagram, scanned text, photo) - despite the name, this
    # is VLM-produced text, not raw OCR (see vision.extractor).
    ocr_text: str | None = None
    extraction_method: Literal["rule_based", "vlm"] | None = None


DocumentElement = Annotated[
    Heading | TextBlock | TableElement | ListElement | ImageElement,
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
    # Row lifecycle timestamps (storage/schema.sql's `documents.created_at`/
    # `updated_at`) - None until the store round-trips a row back
    # (get_document/list_documents); a freshly-parsed Document has no row
    # yet. Distinct from metadata.created_at, which is the source file's own
    # authored-at time, not when this row was written.
    created_at: datetime | None = None
    updated_at: datetime | None = None
