from datetime import datetime, timezone
from pathlib import Path

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.ids import content_hash
from knowledge_forge.models.document import Heading, ImageElement, ListElement, TableElement, TextBlock
from knowledge_forge.models.source import SourceRef, SourceType
from knowledge_forge.parsers.html import HtmlParser

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sample.html"


def _raw_item() -> RawItem:
    data = FIXTURE.read_bytes()
    return RawItem(
        uri=f"file://{FIXTURE}",
        content=data,
        content_hash=content_hash(data),
        modified_at=datetime.now(timezone.utc),
    )


def _source_ref() -> SourceRef:
    return SourceRef(source_id="s1", source_type=SourceType.LOCAL_FILE, uri=f"file://{FIXTURE}")


async def test_html_parses_heading_paragraph_list_table_image():
    document = await HtmlParser().parse(_raw_item(), _source_ref())

    headings = [e for e in document.elements if isinstance(e, Heading)]
    texts = [e for e in document.elements if isinstance(e, TextBlock)]
    lists = [e for e in document.elements if isinstance(e, ListElement)]
    tables = [e for e in document.elements if isinstance(e, TableElement)]
    images = [e for e in document.elements if isinstance(e, ImageElement)]

    assert len(headings) == 1
    assert headings[0].level == 1
    assert headings[0].text == "Knowledge Forge Sample"

    assert len(texts) == 1
    assert texts[0].text == "This is a short paragraph used to exercise the HTML parser."

    assert len(lists) == 1
    assert lists[0].items == ["first item", "second item"]
    assert lists[0].ordered is False

    assert len(tables) == 1
    assert tables[0].rows[0] == ["Name", "Role"]
    assert tables[0].rows[1] == ["Ada", "Engineer"]
    assert tables[0].rows[2] == ["Grace", "Engineer"]

    assert len(images) == 1
    assert images[0].uri == "diagram.png"
    assert images[0].alt_text == "A diagram"


async def test_html_skips_script_and_style_content():
    item = RawItem(
        uri="file://inline.html",
        content=b"<html><head><style>.x{}</style></head>"
        b"<body><script>var x = 1;</script><p>hello</p></body></html>",
        content_hash=content_hash(b"inline"),
        modified_at=datetime.now(timezone.utc),
    )

    document = await HtmlParser().parse(item, _source_ref())

    assert len(document.elements) == 1
    assert isinstance(document.elements[0], TextBlock)
    assert document.elements[0].text == "hello"
