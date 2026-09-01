"""PDF parsing: a hybrid of deterministic text extraction and VLM-based
table/figure transcription.

Running text is still extracted deterministically via pdfplumber (fast,
free, and already reliable). Every detected table and every embedded
image/figure region is instead cropped, rendered to a PNG, and sent
through VisionExtractor (vision.extractor) - tables come back as GFM
markdown and are parsed into TableElement.rows, figures/charts/diagrams
come back as a descriptive paragraph on ImageElement.ocr_text. This is a
deliberate choice (not a fallback-on-failure policy): pdfplumber's own
`extract_tables()` is a decent rectangle-grid heuristic but mangles merged
cells, multi-line cells, and non-gridded tables, and its OCR-free text
extraction obviously can't describe charts/diagrams at all - VLM
transcription is more reliable for exactly the content types that are
hardest for a rule-based parser, which is why they get VLM unconditionally
rather than only when pdfplumber's own attempt looks empty/malformed. If
the VLM call itself fails after retries, each table/figure degrades
independently (pdfplumber's raw cell text for a table, a bare image
element with no description for a figure) rather than failing the whole
document - see _parse_page.

Every table/image element also carries `bbox` (its position on the page)
and `extraction_method` so a Knowledge Object built from it can be traced
back to exactly where on the page it came from.
"""

import io
import logging
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
    ImageElement,
    TableElement,
    TextBlock,
)
from knowledge_forge.models.source import SourceRef
from knowledge_forge.parsers.base import Parser
from knowledge_forge.vision.extractor import VisionExtractor
from knowledge_forge.vision.markdown_table import parse_markdown_table

logger = logging.getLogger(__name__)

# A line is treated as a heading when its dominant font size is at least
# this many times the page's median font size. This is a typographic
# approximation, not semantic understanding - PDFs without a consistent
# larger/bolder-heading convention will not be detected correctly, and
# that is a known, accepted limitation for the MVP (see wiki/parsers.md).
_HEADING_SIZE_RATIO = 1.2
# A vertical gap this many times the page's median line-to-line gap is
# treated as a paragraph break (PDFs have no blank-line markup to split on).
_PARAGRAPH_GAP_RATIO = 1.6
# A text line is considered "already covered" by a table/figure crop (and
# so excluded from plain-text extraction, to avoid transcribing the same
# content twice) when at least this fraction of its area falls inside the
# region's bbox.
_COVERAGE_OVERLAP_THRESHOLD = 0.5
_RENDER_RESOLUTION = 150


def _bbox_overlap_fraction(line_bbox: tuple[float, float, float, float], region_bbox: tuple[float, float, float, float]) -> float:
    lx0, ltop, lx1, lbottom = line_bbox
    rx0, rtop, rx1, rbottom = region_bbox
    ix0, ix1 = max(lx0, rx0), min(lx1, rx1)
    itop, ibottom = max(ltop, rtop), min(lbottom, rbottom)
    if ix1 <= ix0 or ibottom <= itop:
        return 0.0
    inter_area = (ix1 - ix0) * (ibottom - itop)
    line_area = max(lx1 - lx0, 1e-6) * max(lbottom - ltop, 1e-6)
    return inter_area / line_area


class PDFParser(Parser):
    """Parses PDF into a Document using pdfplumber (MIT-licensed - PyMuPDF
    is AGPL-3.0 and was avoided for that reason, see wiki/parsers.md)."""

    supported_extensions = {".pdf"}

    def __init__(self, vision_extractor: VisionExtractor | None = None):
        # Lazily constructed if not injected, so a PDFParser() with no
        # tables/images in the document never has to touch Anthropic
        # credentials at all.
        self._vision = vision_extractor

    def _vision_extractor(self) -> VisionExtractor:
        if self._vision is None:
            self._vision = VisionExtractor()
        return self._vision

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

    def _render_region(self, page, bbox: tuple[float, float, float, float]) -> bytes:
        cropped = page.crop(bbox).to_image(resolution=_RENDER_RESOLUTION)
        buf = io.BytesIO()
        cropped.original.save(buf, format="PNG")
        return buf.getvalue()

    def _parse_tables(self, page, uri: str, start_index: int, elements: list[DocumentElement]) -> list[tuple[float, float, float, float]]:
        covered: list[tuple[float, float, float, float]] = []
        for idx, table in enumerate(page.find_tables()):
            bbox = tuple(table.bbox)
            covered.append(bbox)
            try:
                markdown = self._vision_extractor().extract_table_markdown(self._render_region(page, bbox))
                rows = parse_markdown_table(markdown)
                extraction_method = "vlm"
            except Exception as exc:  # noqa: BLE001 - degrade this table, not the whole document
                logger.warning("VLM table extraction failed on %s page %d table %d, falling back to pdfplumber: %s", uri, page.page_number, idx, exc)
                raw_rows = table.extract() or []
                rows = [["" if cell is None else str(cell) for cell in row] for row in raw_rows]
                extraction_method = "rule_based"
            if not rows:
                continue
            elements.append(
                TableElement(
                    element_id=stable_id("element", uri, str(start_index + len(elements)), "table", str(idx)),
                    page=page.page_number,
                    bbox=bbox,
                    rows=rows,
                    extraction_method=extraction_method,
                )
            )
        return covered

    def _parse_images(self, page, uri: str, start_index: int, elements: list[DocumentElement]) -> list[tuple[float, float, float, float]]:
        covered: list[tuple[float, float, float, float]] = []
        for idx, img in enumerate(page.images):
            bbox = (img["x0"], img["top"], img["x1"], img["bottom"])
            covered.append(bbox)
            description = None
            try:
                description = self._vision_extractor().describe_figure(self._render_region(page, bbox)) or None
            except Exception as exc:  # noqa: BLE001 - degrade this image, not the whole document
                logger.warning("VLM figure description failed on %s page %d image %d: %s", uri, page.page_number, idx, exc)
            elements.append(
                ImageElement(
                    element_id=stable_id("element", uri, str(start_index + len(elements)), "image", str(idx)),
                    page=page.page_number,
                    bbox=bbox,
                    ocr_text=description,
                    extraction_method="vlm" if description is not None else None,
                )
            )
        return covered

    def _parse_page(self, page, uri: str, start_index: int) -> list[DocumentElement]:
        elements: list[DocumentElement] = []
        page_number = page.page_number

        covered_bboxes = self._parse_tables(page, uri, start_index, elements)
        covered_bboxes += self._parse_images(page, uri, start_index, elements)

        lines = page.extract_text_lines()
        if not lines:
            return elements

        def is_covered(line: dict) -> bool:
            line_bbox = (line["x0"], line["top"], line["x1"], line["bottom"])
            return any(
                _bbox_overlap_fraction(line_bbox, region) >= _COVERAGE_OVERLAP_THRESHOLD
                for region in covered_bboxes
            )

        lines = [line for line in lines if not is_covered(line)]
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
                        bbox=(line["x0"], line["top"], line["x1"], line["bottom"]),
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
