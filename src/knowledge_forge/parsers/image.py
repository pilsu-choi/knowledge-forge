from knowledge_forge.collectors.base import RawItem
from knowledge_forge.models.document import Document
from knowledge_forge.models.source import SourceRef
from knowledge_forge.parsers.base import Parser


class ImageParser(Parser):
    """Not yet implemented.

    Intended approach: `Pillow` + `pytesseract` (already optional
    dependencies, the `image` extra). `Image.open(io.BytesIO(item.content))`,
    run `pytesseract.image_to_string(img, lang="eng+kor")`, and emit a
    single `ImageElement(uri=item.uri, ocr_text=...)`. Requires the system
    `tesseract-ocr` binary, plus the `tesseract-ocr-kor` language pack for
    Korean text - these are not Python packages and must be installed at
    the OS level.
    """

    supported_extensions = {".png", ".jpg", ".jpeg"}

    def parse(self, item: RawItem, source_ref: SourceRef) -> Document:
        raise NotImplementedError(
            "ImageParser is a stub. Implement with Pillow + pytesseract: "
            "pytesseract.image_to_string(Image.open(io.BytesIO(item.content)), "
            "lang='eng+kor') -> single ImageElement(ocr_text=...). "
            "Requires system tesseract-ocr (+ tesseract-ocr-kor for Korean)."
        )
