from datetime import datetime, timezone
from pathlib import Path

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.ids import content_hash
from knowledge_forge.models.document import Heading, ImageElement, TableElement, TextBlock
from knowledge_forge.models.source import SourceRef, SourceType
from knowledge_forge.parsers.pptx import PptxParser
from knowledge_forge.vision.extractor import VisionExtractor

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sample.pptx"

_FIGURE_DESCRIPTION = "A solid blue rectangle placeholder image."


class _FakeVisionExtractor(VisionExtractor):
    def __init__(self):
        self.figure_calls = 0

    def describe_figure(self, image_bytes: bytes, media_type: str = "image/png") -> str:
        self.figure_calls += 1
        return _FIGURE_DESCRIPTION


def test_pptx_parses_title_body_table_and_picture_across_slides():
    data = FIXTURE.read_bytes()
    item = RawItem(
        uri=f"file://{FIXTURE}",
        content=data,
        content_hash=content_hash(data),
        modified_at=datetime.now(timezone.utc),
    )
    source_ref = SourceRef(source_id="s1", source_type=SourceType.LOCAL_FILE, uri=item.uri)

    vision = _FakeVisionExtractor()
    document = PptxParser(vision_extractor=vision).parse(item, source_ref)

    headings = [e for e in document.elements if isinstance(e, Heading)]
    texts = [e for e in document.elements if isinstance(e, TextBlock)]
    tables = [e for e in document.elements if isinstance(e, TableElement)]
    images = [e for e in document.elements if isinstance(e, ImageElement)]

    assert len(headings) == 1
    assert headings[0].text == "Knowledge Forge Sample"
    assert headings[0].page == 1

    assert len(texts) == 1
    assert "body paragraph" in texts[0].text
    assert texts[0].page == 1

    assert len(tables) == 1
    assert tables[0].page == 2
    assert tables[0].rows[0] == ["Name", "Role"]
    assert tables[0].rows[1] == ["Ada", "Engineer"]

    # slide 3's picture shape is described via VLM, not left unparsed.
    assert len(images) == 1
    assert images[0].page == 3
    assert images[0].extraction_method == "vlm"
    assert images[0].ocr_text == _FIGURE_DESCRIPTION
    assert vision.figure_calls == 1
