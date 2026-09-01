import asyncio
import logging
import re

from hwpx.document import HwpxDocument

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.exceptions import ParsingError
from knowledge_forge.ids import stable_id
from knowledge_forge.models.document import Document, DocumentElement, Heading, ImageElement, TableElement, TextBlock
from knowledge_forge.models.source import SourceRef
from knowledge_forge.parsers.base import Parser
from knowledge_forge.vision.extractor import VisionExtractor
from knowledge_forge.vision.factory import get_vision_extractor

logger = logging.getLogger(__name__)

# HWP's built-in heading styles are named "Outline N" (eng_name) / "개요 N"
# (name) - the same "style name encodes heading level" convention DOCX uses
# for "Heading N".
_OUTLINE_EN_RE = re.compile(r"^Outline\s+(\d+)$", re.IGNORECASE)
_OUTLINE_KO_RE = re.compile(r"^개요\s*(\d+)$")

# HWPX stores a paragraph's left margin in HWPUNIT (1/7200 inch). This
# bucket size approximates one "increase indent" step (~3mm) - a coarse
# layout heuristic, same spirit as DOCX's EMU indent step and PDF's
# x-position one, not exact semantic list-nesting detection.
_HWPUNIT_PER_INDENT_LEVEL = 850


def _heading_level(style) -> int | None:
    for pattern, name in ((_OUTLINE_EN_RE, style.eng_name), (_OUTLINE_KO_RE, style.name)):
        if not name:
            continue
        match = pattern.match(name)
        if match:
            return max(1, min(int(match.group(1)), 6))
    return None


def _indent_level(paragraph_property) -> int | None:
    """`paragraph_property` is a `doc.styles.paragraph_property(...)` result
    (or None). Some HWPX files wrap `margin` in a `hp:switch`/`hp:case` block
    that python-hwpx doesn't currently surface here - this degrades to None
    (no indent signal) rather than raising in that case.
    """
    margin = paragraph_property.margin if paragraph_property is not None else None
    left = margin.left if margin is not None else None
    if not left:
        return None
    level = round(left / _HWPUNIT_PER_INDENT_LEVEL)
    return level if level >= 1 else None


class HwpxParser(Parser):
    """Parses .hwpx into a Document using python-hwpx.

    HWPX has no page concept at the XML level (pagination is a rendering
    detail), same reasoning as DOCX, so every element's `page` stays None.
    Tables are extracted deterministically (cell text is exact, same as
    DOCX/PPTX tables - no VLM needed). Embedded images go through
    VisionExtractor, same policy and per-image degradation as PPTX's
    Picture shapes: one bad image doesn't fail the whole document.
    """

    supported_extensions = {".hwpx"}

    def __init__(self, vision_extractor: VisionExtractor | None = None):
        self._vision = vision_extractor

    def _vision_extractor(self) -> VisionExtractor:
        if self._vision is None:
            self._vision = get_vision_extractor()
        return self._vision

    async def parse(self, item: RawItem, source_ref: SourceRef) -> Document:
        try:
            doc = HwpxDocument.open(item.content)
            elements: list[DocumentElement] = []
            elements.extend(self._convert_paragraphs(doc, item.uri))
            elements.extend(self._convert_tables(doc, item.uri, len(elements)))
            elements.extend(await self._convert_images(doc, item.uri, len(elements)))
        except Exception as exc:  # noqa: BLE001 - surfaced as a typed parsing error
            raise ParsingError(f"Failed to parse hwpx '{item.uri}': {exc}") from exc

        return Document(
            document_id=stable_id("document", item.uri, item.content_hash),
            source=source_ref,
            elements=elements,
        )

    def _convert_paragraphs(self, doc: HwpxDocument, uri: str) -> list[DocumentElement]:
        elements: list[DocumentElement] = []
        for paragraph in doc.paragraphs:
            text = paragraph.text.strip()
            if not text:
                continue

            style = doc.styles.get(paragraph.style_id_ref)
            level = _heading_level(style) if style is not None else None
            try:
                paragraph_property = doc.styles.paragraph_property(paragraph.para_pr_id_ref)
            except KeyError:
                paragraph_property = None
            indent = _indent_level(paragraph_property)
            if level is not None:
                elements.append(
                    Heading(
                        element_id=stable_id("element", uri, str(len(elements)), "heading"),
                        level=level,
                        text=text,
                        indent_level=indent,
                    )
                )
            else:
                elements.append(
                    TextBlock(
                        element_id=stable_id("element", uri, str(len(elements)), "text"),
                        text=text,
                        indent_level=indent,
                    )
                )
        return elements

    def _convert_tables(self, doc: HwpxDocument, uri: str, start_index: int) -> list[DocumentElement]:
        elements: list[DocumentElement] = []
        for table in doc.tables:
            rows = [
                [table.cell(r, c).text for c in range(table.column_count)]
                for r in range(table.row_count)
            ]
            elements.append(
                TableElement(
                    element_id=stable_id("element", uri, str(start_index + len(elements)), "table"),
                    rows=rows,
                    extraction_method="rule_based",
                )
            )
        return elements

    async def _convert_images(self, doc: HwpxDocument, uri: str, start_index: int) -> list[DocumentElement]:
        images = list(doc.media.images)

        async def _describe(item) -> str | None:
            try:
                image_bytes = doc.package.read(item.href)
                return await self._vision_extractor().describe_figure(
                    image_bytes, media_type=f"image/{item.format}"
                ) or None
            except Exception as exc:  # noqa: BLE001 - degrade this image, not the whole document
                logger.warning("VLM figure description failed on %s image %s: %s", uri, item.item_id, exc)
                return None

        descriptions = await asyncio.gather(*(_describe(item) for item in images))

        elements: list[DocumentElement] = []
        for description in descriptions:
            elements.append(
                ImageElement(
                    element_id=stable_id("element", uri, str(start_index + len(elements)), "image"),
                    ocr_text=description,
                    extraction_method="vlm" if description is not None else None,
                )
            )
        return elements
