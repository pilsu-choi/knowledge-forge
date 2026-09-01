import io
import statistics

import pdfplumber

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.exceptions import ParsingError
from knowledge_forge.ids import stable_id
from knowledge_forge.models.document import (
    Document,
    DocumentElement,
    DocumentMetadata,
    Heading,
    TableElement,
    TextBlock,
)
from knowledge_forge.models.source import SourceRef
from knowledge_forge.parsers.base import Parser

# A line is treated as a heading when its dominant font size is at least
# this many times the page's median font size. This is a typographic
# approximation, not semantic understanding - PDFs without a consistent
# larger/bolder-heading convention will not be detected correctly, and
# that is a known, accepted limitation for the MVP (see wiki/parsers.md).
_HEADING_SIZE_RATIO = 1.2
# A vertical gap this many times the page's median line-to-line gap is
# treated as a paragraph break (PDFs have no blank-line markup to split on).
_PARAGRAPH_GAP_RATIO = 1.6


class PDFParser(Parser):
    """Parses PDF into a Document using pdfplumber (MIT-licensed - PyMuPDF
    is AGPL-3.0 and was avoided for that reason, see wiki/parsers.md)."""

    supported_extensions = {".pdf"}

    def parse(self, item: RawItem, source_ref: SourceRef) -> Document:
        try:
            elements: list[DocumentElement] = []
            with pdfplumber.open(io.BytesIO(item.content)) as pdf:
                page_count = len(pdf.pages)
                for page in pdf.pages:
                    elements.extend(self._parse_page(page, item.uri, len(elements)))
        except Exception as exc:  # noqa: BLE001 - surfaced as a typed parsing error
            raise ParsingError(f"Failed to parse pdf '{item.uri}': {exc}") from exc

        return Document(
            document_id=stable_id("document", item.uri, item.content_hash),
            source=source_ref,
            elements=elements,
            metadata=DocumentMetadata(page_count=page_count),
        )

    def _parse_page(self, page, uri: str, start_index: int) -> list[DocumentElement]:
        elements: list[DocumentElement] = []
        page_number = page.page_number

        for table_index, rows in enumerate(page.extract_tables()):
            elements.append(
                TableElement(
                    element_id=stable_id(
                        "element", uri, str(start_index + len(elements)), "table", str(table_index)
                    ),
                    page=page_number,
                    rows=[["" if cell is None else str(cell) for cell in row] for row in rows],
                )
            )

        lines = page.extract_text_lines()
        if not lines:
            return elements

        sizes = [
            statistics.median(ch["size"] for ch in line["chars"]) if line["chars"] else 0.0
            for line in lines
        ]
        page_median_size = statistics.median(s for s in sizes if s > 0) if any(sizes) else 0.0
        gaps = [b["top"] - a["bottom"] for a, b in zip(lines, lines[1:])]
        median_gap = statistics.median(gaps) if gaps else 0.0

        paragraph_buffer: list[str] = []

        def flush_paragraph() -> None:
            if paragraph_buffer:
                elements.append(
                    TextBlock(
                        element_id=stable_id(
                            "element", uri, str(start_index + len(elements)), "text"
                        ),
                        page=page_number,
                        text=" ".join(paragraph_buffer),
                    )
                )
                paragraph_buffer.clear()

        for i, line in enumerate(lines):
            text = line["text"].strip()
            if not text:
                continue

            is_heading = page_median_size > 0 and sizes[i] >= page_median_size * _HEADING_SIZE_RATIO
            if is_heading:
                flush_paragraph()
                elements.append(
                    Heading(
                        element_id=stable_id(
                            "element", uri, str(start_index + len(elements)), "heading"
                        ),
                        page=page_number,
                        level=1,
                        text=text,
                    )
                )
                continue

            if i > 0 and median_gap > 0:
                gap = line["top"] - lines[i - 1]["bottom"]
                if gap > median_gap * _PARAGRAPH_GAP_RATIO:
                    flush_paragraph()

            paragraph_buffer.append(text)

        flush_paragraph()
        return elements
