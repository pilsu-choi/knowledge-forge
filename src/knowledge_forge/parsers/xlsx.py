import asyncio
import io
import logging

import openpyxl

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.exceptions import ParsingError
from knowledge_forge.ids import stable_id
from knowledge_forge.models.document import Document, DocumentElement, ImageElement, TableElement
from knowledge_forge.models.source import SourceRef
from knowledge_forge.parsers.base import Parser
from knowledge_forge.vision.extractor import VisionExtractor
from knowledge_forge.vision.factory import get_vision_extractor

logger = logging.getLogger(__name__)


class XlsxParser(Parser):
    """Parses .xlsx into a Document using openpyxl - one TableElement per
    worksheet, sheet name as the table's caption. Embedded images (anchored
    over or "in" a cell - XLSX has no other kind `openpyxl` can read) go
    through VisionExtractor, same policy and per-image degradation as
    DOCX/PPTX/HWPX.

    XLSX has no page concept - every element's `page` stays None.
    """

    supported_extensions = {".xlsx"}

    def __init__(self, vision_extractor: VisionExtractor | None = None):
        self._vision = vision_extractor

    def _vision_extractor(self) -> VisionExtractor:
        if self._vision is None:
            self._vision = get_vision_extractor()
        return self._vision

    async def parse(self, item: RawItem, source_ref: SourceRef) -> Document:
        try:
            elements: list[DocumentElement] = []
            workbook = openpyxl.load_workbook(io.BytesIO(item.content), data_only=True)
            for sheet in workbook.worksheets:
                rows = [
                    ["" if cell is None else str(cell) for cell in row]
                    for row in sheet.iter_rows(values_only=True)
                ]
                if rows:
                    elements.append(
                        TableElement(
                            element_id=stable_id("element", item.uri, str(len(elements)), "table"),
                            rows=rows,
                            caption=sheet.title,
                            extraction_method="rule_based",
                        )
                    )
                elements.extend(await self._convert_images(sheet, item.uri, len(elements)))
        except Exception as exc:  # noqa: BLE001 - surfaced as a typed parsing error
            raise ParsingError(f"Failed to parse xlsx '{item.uri}': {exc}") from exc

        return Document(
            document_id=stable_id("document", item.uri, item.content_hash),
            source=source_ref,
            elements=elements,
        )

    async def _convert_images(self, sheet, uri: str, start_index: int) -> list[DocumentElement]:
        images = list(sheet._images)  # noqa: SLF001 - openpyxl exposes no public accessor

        async def _describe(image) -> str | None:
            try:
                image_bytes = image._data()  # noqa: SLF001 - openpyxl exposes no public accessor
                return await self._vision_extractor().describe_figure(
                    image_bytes, media_type=f"image/{image.format}"
                ) or None
            except Exception as exc:  # noqa: BLE001 - degrade this image, not the whole document
                logger.warning("VLM figure description failed on %s sheet %s: %s", uri, sheet.title, exc)
                return None

        descriptions = await asyncio.gather(*(_describe(image) for image in images))

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
