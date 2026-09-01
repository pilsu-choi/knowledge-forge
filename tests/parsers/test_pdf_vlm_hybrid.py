"""Tests for the VLM-hybrid table/figure path in parsers/pdf.py, using a
fixture with a real gridded table and an embedded image
(tests/fixtures/sample_with_table.pdf) and a fake VisionExtractor (no
Anthropic credentials needed).
"""

from datetime import datetime, timezone
from pathlib import Path

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.ids import content_hash
from knowledge_forge.models.document import Heading, ImageElement, TableElement, TextBlock
from knowledge_forge.models.source import SourceRef, SourceType
from knowledge_forge.parsers.pdf import PDFParser
from knowledge_forge.vision.extractor import VisionExtractor

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sample_with_table.pdf"

_TABLE_MARKDOWN = "| Company | Sales | Growth |\n|---|---|---|\n| A | 100M | 20% |\n| B | 80M | 15% |"
_FIGURE_DESCRIPTION = "A solid red rectangle placeholder image."


class _FakeVisionExtractor(VisionExtractor):
    def __init__(self):
        self.table_calls = 0
        self.figure_calls = 0

    async def extract_table_markdown(self, image_bytes: bytes, media_type: str = "image/png") -> str:
        self.table_calls += 1
        return _TABLE_MARKDOWN

    async def describe_figure(self, image_bytes: bytes, media_type: str = "image/png") -> str:
        self.figure_calls += 1
        return _FIGURE_DESCRIPTION


async def _parse(vision):
    data = FIXTURE.read_bytes()
    item = RawItem(
        uri=f"file://{FIXTURE}",
        content=data,
        content_hash=content_hash(data),
        modified_at=datetime.now(timezone.utc),
    )
    source_ref = SourceRef(source_id="s1", source_type=SourceType.LOCAL_FILE, uri=item.uri)
    return await PDFParser(vision_extractor=vision).parse(item, source_ref)


async def test_table_is_extracted_via_vlm_and_parsed_into_rows():
    vision = _FakeVisionExtractor()
    document = await _parse(vision)

    tables = [e for e in document.elements if isinstance(e, TableElement)]
    assert len(tables) == 1
    assert tables[0].extraction_method == "vlm"
    assert tables[0].rows == [["Company", "Sales", "Growth"], ["A", "100M", "20%"], ["B", "80M", "15%"]]
    assert tables[0].bbox is not None
    assert vision.table_calls == 1


async def test_image_is_described_via_vlm():
    vision = _FakeVisionExtractor()
    document = await _parse(vision)

    images = [e for e in document.elements if isinstance(e, ImageElement)]
    assert len(images) == 1
    assert images[0].extraction_method == "vlm"
    assert images[0].ocr_text == _FIGURE_DESCRIPTION
    assert images[0].bbox is not None
    assert vision.figure_calls == 1


async def test_table_cell_text_is_not_duplicated_as_plain_text_blocks():
    document = await _parse(_FakeVisionExtractor())

    texts = [e for e in document.elements if isinstance(e, TextBlock)]
    combined = " ".join(t.text for t in texts)
    # The raw table cell text ("Company", "100M", "20%", ...) must not
    # reappear as plain paragraph text - it was already handled by the VLM
    # table element and excluded from text-line extraction.
    assert "100M" not in combined
    assert "20%" not in combined

    headings = [e for e in document.elements if isinstance(e, Heading)]
    assert any(h.text == "Revenue Report" for h in headings)
    assert any("quarterly revenue" in t.text for t in texts)


async def test_vlm_failure_falls_back_to_pdfplumber_table_extraction():
    class _FailingVision(VisionExtractor):
        def __init__(self):
            pass

        async def extract_table_markdown(self, image_bytes, media_type="image/png"):
            raise RuntimeError("simulated VLM outage")

        async def describe_figure(self, image_bytes, media_type="image/png"):
            raise RuntimeError("simulated VLM outage")

    document = await _parse(_FailingVision())

    tables = [e for e in document.elements if isinstance(e, TableElement)]
    assert len(tables) == 1
    assert tables[0].extraction_method == "rule_based"
    assert tables[0].rows  # pdfplumber's own extraction still produced something

    images = [e for e in document.elements if isinstance(e, ImageElement)]
    assert len(images) == 1
    assert images[0].extraction_method is None
    assert images[0].ocr_text is None
