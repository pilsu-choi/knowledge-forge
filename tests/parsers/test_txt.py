from datetime import datetime, timezone
from pathlib import Path

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.ids import content_hash
from knowledge_forge.models.document import TextBlock
from knowledge_forge.models.source import SourceRef, SourceType
from knowledge_forge.parsers.txt import TxtParser

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sample.txt"


async def test_txt_splits_into_paragraphs_by_blank_line():
    data = FIXTURE.read_bytes()
    item = RawItem(
        uri=f"file://{FIXTURE}",
        content=data,
        content_hash=content_hash(data),
        modified_at=datetime.now(timezone.utc),
    )
    source_ref = SourceRef(source_id="s1", source_type=SourceType.LOCAL_FILE, uri=item.uri)

    document = await TxtParser().parse(item, source_ref)

    assert len(document.elements) == 2
    assert all(isinstance(e, TextBlock) for e in document.elements)
    assert "first paragraph" in document.elements[0].text
    assert "second paragraph" in document.elements[1].text
    assert all(e.page is None for e in document.elements)
