import io

from docx import Document as DocxDocument

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.exceptions import ParsingError
from knowledge_forge.ids import stable_id
from knowledge_forge.models.document import (
    Document,
    DocumentElement,
    Heading,
    ListElement,
    TableElement,
    TextBlock,
)
from knowledge_forge.models.source import SourceRef
from knowledge_forge.parsers.base import Parser

_LIST_STYLES = {"List Bullet", "List Number"}


class DocxParser(Parser):
    """Parses .docx into a Document using python-docx.

    DOCX has no page concept at the XML level (pagination is a rendering
    detail), so every element's `page` stays None.
    """

    supported_extensions = {".docx"}

    def parse(self, item: RawItem, source_ref: SourceRef) -> Document:
        try:
            docx_document = DocxDocument(io.BytesIO(item.content))
            elements = self._convert_paragraphs(docx_document, item.uri)
            elements.extend(self._convert_tables(docx_document, item.uri, len(elements)))
        except Exception as exc:  # noqa: BLE001 - surfaced as a typed parsing error
            raise ParsingError(f"Failed to parse docx '{item.uri}': {exc}") from exc

        return Document(
            document_id=stable_id("document", item.uri, item.content_hash),
            source=source_ref,
            elements=elements,
        )

    def _convert_paragraphs(self, docx_document: DocxDocument, uri: str) -> list[DocumentElement]:
        elements: list[DocumentElement] = []
        pending_list_items: list[str] = []
        pending_list_ordered = False

        def flush_list() -> None:
            nonlocal pending_list_items, pending_list_ordered
            if pending_list_items:
                elements.append(
                    ListElement(
                        element_id=stable_id("element", uri, str(len(elements)), "list"),
                        items=pending_list_items,
                        ordered=pending_list_ordered,
                    )
                )
                pending_list_items = []

        for paragraph in docx_document.paragraphs:
            style_name = paragraph.style.name if paragraph.style else "Normal"
            text = paragraph.text.strip()

            if style_name in _LIST_STYLES:
                if not text:
                    continue
                pending_list_ordered = style_name == "List Number"
                pending_list_items.append(text)
                continue

            flush_list()

            if not text:
                continue

            if style_name.startswith("Heading"):
                level_char = style_name[-1]
                level = int(level_char) if level_char.isdigit() else 1
                elements.append(
                    Heading(
                        element_id=stable_id("element", uri, str(len(elements)), "heading"),
                        level=max(1, min(level, 6)),
                        text=text,
                    )
                )
            else:
                elements.append(
                    TextBlock(
                        element_id=stable_id("element", uri, str(len(elements)), "text"),
                        text=text,
                    )
                )

        flush_list()
        return elements

    def _convert_tables(
        self, docx_document: DocxDocument, uri: str, start_index: int
    ) -> list[DocumentElement]:
        elements: list[DocumentElement] = []
        for offset, table in enumerate(docx_document.tables):
            rows = [[cell.text for cell in row.cells] for row in table.rows]
            elements.append(
                TableElement(
                    element_id=stable_id("element", uri, str(start_index + offset), "table"),
                    rows=rows,
                )
            )
        return elements
