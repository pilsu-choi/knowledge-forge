from datetime import datetime, timezone
from pathlib import Path

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.ids import content_hash
from knowledge_forge.models.document import Heading, ListElement, TableElement, TextBlock
from knowledge_forge.models.source import SourceRef, SourceType
from knowledge_forge.parsers.docx import DocxParser

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sample.docx"


def test_docx_parses_heading_paragraph_list_table():
    data = FIXTURE.read_bytes()
    item = RawItem(
        uri=f"file://{FIXTURE}",
        content=data,
        content_hash=content_hash(data),
        modified_at=datetime.now(timezone.utc),
    )
    source_ref = SourceRef(source_id="s1", source_type=SourceType.LOCAL_FILE, uri=item.uri)

    document = DocxParser().parse(item, source_ref)

    headings = [e for e in document.elements if isinstance(e, Heading)]
    texts = [e for e in document.elements if isinstance(e, TextBlock)]
    lists = [e for e in document.elements if isinstance(e, ListElement)]
    tables = [e for e in document.elements if isinstance(e, TableElement)]

    assert len(headings) == 1
    assert headings[0].level == 1
    assert headings[0].text == "Knowledge Forge Sample"

    assert len(texts) == 1
    assert "short paragraph" in texts[0].text

    assert len(lists) == 1
    assert lists[0].items == ["first item", "second item"]

    assert len(tables) == 1
    assert tables[0].rows[0] == ["Name", "Role"]
    assert tables[0].rows[1] == ["Ada", "Engineer"]

    assert all(e.page is None for e in document.elements)
