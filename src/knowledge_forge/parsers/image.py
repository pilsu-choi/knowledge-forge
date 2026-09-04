"""Standalone image file parsing via Claude vision (VisionExtractor),
replacing OCR - a VLM both transcribes text in the image and, unlike OCR,
can describe a chart/diagram/photo that has little or no text at all.
"""

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.exceptions import ParsingError
from knowledge_forge.ids import stable_id
from knowledge_forge.models.document import Document, ImageElement
from knowledge_forge.models.source import SourceRef
from knowledge_forge.parsers.base import Parser
from knowledge_forge.vision.factory import VisionExtractorLike, get_vision_extractor

_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


class ImageParser(Parser):
    """Parses a standalone image file into a Document via VisionExtractor.

    A page/image that transcribes to no meaningful content still produces
    one ImageElement with `ocr_text=None`, not a parsing failure - "an
    image with no extractable content" is a valid, meaningful result. A
    VLM failure (after VisionExtractor's own retries) is a real parsing
    failure for a single-image document, unlike PDF's per-region
    degradation - there's no deterministic fallback for a whole photo.
    """

    supported_extensions = {".png", ".jpg", ".jpeg"}

    def __init__(self, vision_extractor: VisionExtractorLike | None = None):
        self._vision = vision_extractor

    def _vision_extractor(self) -> VisionExtractorLike:
        if self._vision is None:
            self._vision = get_vision_extractor()
        return self._vision

    async def parse(self, item: RawItem, source_ref: SourceRef) -> Document:
        extension = next((ext for ext in self.supported_extensions if item.uri.lower().endswith(ext)), ".png")
        media_type = _MEDIA_TYPES.get(extension, "image/png")

        try:
            text = await self._vision_extractor().transcribe_image(item.content, media_type=media_type)
        except Exception as exc:  # noqa: BLE001 - surfaced as a typed parsing error
            raise ParsingError(f"Failed to parse image '{item.uri}': {exc}") from exc

        element = ImageElement(
            element_id=stable_id("element", item.uri, "0", "image"),
            uri=item.uri,
            ocr_text=text or None,
            extraction_method="vlm",
        )

        return Document(
            document_id=stable_id("document", item.uri, item.content_hash),
            source=source_ref,
            elements=[element],
        )
