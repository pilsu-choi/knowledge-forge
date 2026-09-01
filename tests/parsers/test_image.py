from datetime import datetime, timezone
from pathlib import Path

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.ids import content_hash
from knowledge_forge.models.document import ImageElement
from knowledge_forge.models.source import SourceRef, SourceType
from knowledge_forge.parsers.image import ImageParser
from knowledge_forge.vision.extractor import VisionExtractor

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sample.png"


class _FakeVisionExtractor(VisionExtractor):
    def __init__(self, result: str):
        self._result = result

    def transcribe_image(self, image_bytes: bytes, media_type: str = "image/png") -> str:
        return self._result


def _item() -> RawItem:
    data = FIXTURE.read_bytes()
    return RawItem(
        uri=f"file://{FIXTURE}",
        content=data,
        content_hash=content_hash(data),
        modified_at=datetime.now(timezone.utc),
    )


def test_image_parser_wires_vlm_transcription_into_image_element():
    item = _item()
    source_ref = SourceRef(source_id="s1", source_type=SourceType.LOCAL_FILE, uri=item.uri)

    document = ImageParser(vision_extractor=_FakeVisionExtractor("hello world")).parse(item, source_ref)

    images = [e for e in document.elements if isinstance(e, ImageElement)]
    assert len(images) == 1
    assert images[0].ocr_text == "hello world"
    assert images[0].uri == item.uri
    assert images[0].extraction_method == "vlm"


def test_image_parser_handles_empty_vlm_result_without_crashing():
    item = _item()
    source_ref = SourceRef(source_id="s1", source_type=SourceType.LOCAL_FILE, uri=item.uri)

    document = ImageParser(vision_extractor=_FakeVisionExtractor("")).parse(item, source_ref)

    images = [e for e in document.elements if isinstance(e, ImageElement)]
    assert len(images) == 1
    assert images[0].ocr_text is None
