from datetime import datetime, timezone
from pathlib import Path

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.ids import content_hash
from knowledge_forge.models.document import ImageElement, TableElement
from knowledge_forge.models.source import SourceRef, SourceType
from knowledge_forge.parsers.xlsx import XlsxParser
from knowledge_forge.vision.extractor import VisionExtractor

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sample.xlsx"

_FIGURE_DESCRIPTION = "A solid blue rectangle placeholder image."


class _FakeVisionExtractor(VisionExtractor):
    def __init__(self):
        self.figure_calls = 0

    async def describe_figure(self, image_bytes: bytes, media_type: str = "image/png") -> str:
        self.figure_calls += 1
        return _FIGURE_DESCRIPTION


async def test_xlsx_parses_sheet_into_a_table_element_and_embedded_image():
    data = FIXTURE.read_bytes()
    item = RawItem(
        uri=f"file://{FIXTURE}",
        content=data,
        content_hash=content_hash(data),
        modified_at=datetime.now(timezone.utc),
    )
    source_ref = SourceRef(source_id="s1", source_type=SourceType.LOCAL_FILE, uri=item.uri)

    vision = _FakeVisionExtractor()
    document = await XlsxParser(vision_extractor=vision).parse(item, source_ref)

    tables = [e for e in document.elements if isinstance(e, TableElement)]
    images = [e for e in document.elements if isinstance(e, ImageElement)]

    assert len(tables) == 1
    assert tables[0].caption == "Sheet1"
    assert tables[0].rows[0] == ["id", "name", "amount"]
    assert tables[0].rows[1] == ["1", "Ada", "100"]
    assert tables[0].rows[2] == ["2", "Grace", "200"]
    assert tables[0].extraction_method == "rule_based"
    # no merged multi-level header or formulas in this fixture - defaults hold.
    assert tables[0].header_rows == 1
    assert tables[0].structured is None
    assert tables[0].formulas is None

    # the embedded picture anchored on the sheet is described via VLM, not
    # left unparsed.
    assert len(images) == 1
    assert images[0].extraction_method == "vlm"
    assert images[0].ocr_text == _FIGURE_DESCRIPTION
    assert vision.figure_calls == 1

    assert all(e.page is None for e in document.elements)
