from datetime import datetime, timezone
from pathlib import Path

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.ids import content_hash
from knowledge_forge.models.document import Heading, ImageElement, TableElement, TextBlock
from knowledge_forge.models.source import SourceRef, SourceType
from knowledge_forge.parsers.hwpx import HwpxParser, _heading_level
from knowledge_forge.vision.extractor import VisionExtractor

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sample.hwpx"

_FIGURE_DESCRIPTION = "A solid blue rectangle placeholder image."


class _FakeStyle:
    def __init__(self, eng_name: str | None, name: str | None):
        self.eng_name = eng_name
        self.name = name


class _FakeVisionExtractor(VisionExtractor):
    def __init__(self):
        self.figure_calls = 0

    def describe_figure(self, image_bytes: bytes, media_type: str = "image/png") -> str:
        self.figure_calls += 1
        return _FIGURE_DESCRIPTION


def test_heading_level_detects_english_and_korean_outline_styles():
    assert _heading_level(_FakeStyle("Outline 1", "개요 1")) == 1
    assert _heading_level(_FakeStyle("Outline 3", "개요 3")) == 3
    assert _heading_level(_FakeStyle(None, "개요 2")) == 2
    assert _heading_level(_FakeStyle("Normal", "바탕글")) is None
    assert _heading_level(_FakeStyle("Outline 9", "개요 9")) == 6  # clamped to 6


def test_hwpx_parses_heading_paragraph_table_and_image():
    data = FIXTURE.read_bytes()
    item = RawItem(
        uri=f"file://{FIXTURE}",
        content=data,
        content_hash=content_hash(data),
        modified_at=datetime.now(timezone.utc),
    )
    source_ref = SourceRef(source_id="s1", source_type=SourceType.LOCAL_FILE, uri=item.uri)

    vision = _FakeVisionExtractor()
    document = HwpxParser(vision_extractor=vision).parse(item, source_ref)

    headings = [e for e in document.elements if isinstance(e, Heading)]
    texts = [e for e in document.elements if isinstance(e, TextBlock)]
    tables = [e for e in document.elements if isinstance(e, TableElement)]
    images = [e for e in document.elements if isinstance(e, ImageElement)]

    assert len(headings) == 1
    assert headings[0].text == "Knowledge Forge Sample"
    assert headings[0].level == 1

    assert len(texts) == 1
    assert "short paragraph" in texts[0].text

    assert len(tables) == 1
    assert tables[0].extraction_method == "rule_based"
    assert tables[0].rows[0] == ["Name", "Role"]
    assert tables[0].rows[1] == ["Ada", "Engineer"]

    assert len(images) == 1
    assert images[0].extraction_method == "vlm"
    assert images[0].ocr_text == _FIGURE_DESCRIPTION
    assert vision.figure_calls == 1
