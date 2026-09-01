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

    async def describe_figure(self, image_bytes: bytes, media_type: str = "image/png") -> str:
        self.figure_calls += 1
        return _FIGURE_DESCRIPTION


async def test_pptx_parses_title_body_table_picture_and_chart_across_slides():
    data = FIXTURE.read_bytes()
    item = RawItem(
        uri=f"file://{FIXTURE}",
        content=data,
        content_hash=content_hash(data),
        modified_at=datetime.now(timezone.utc),
    )
    source_ref = SourceRef(source_id="s1", source_type=SourceType.LOCAL_FILE, uri=item.uri)

    vision = _FakeVisionExtractor()
    document = await PptxParser(vision_extractor=vision).parse(item, source_ref)

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

    # slide 2's native table is rule-based (exact cell text, no VLM needed).
    assert len(tables) == 2
    native_table = next(t for t in tables if t.page == 2)
    assert native_table.rows[0] == ["Name", "Role"]
    assert native_table.rows[1] == ["Ada", "Engineer"]
    assert native_table.extraction_method == "rule_based"

    # slide 3's picture shape is described via VLM, not left unparsed.
    assert len(images) == 1
    assert images[0].page == 3
    assert images[0].extraction_method == "vlm"
    assert images[0].ocr_text == _FIGURE_DESCRIPTION
    assert vision.figure_calls == 1

    # slide 4's native chart object is extracted deterministically (category/
    # series data), not sent through VLM - no rasterizing backend exists here
    # and the numbers are already exact in the chart XML.
    chart_table = next(t for t in tables if t.page == 4)
    assert chart_table.extraction_method == "rule_based"
    assert chart_table.rows[0] == ["", "A", "B"]
    assert chart_table.rows[1] == ["Sales", "100", "80"]
    assert chart_table.caption == "COLUMN_CLUSTERED chart"
