from datetime import datetime, timezone
from pathlib import Path

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.ids import content_hash
from knowledge_forge.models.document import Heading, TextBlock
from knowledge_forge.models.source import SourceRef, SourceType
from knowledge_forge.parsers.pdf import PDFParser

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sample.pdf"


def test_pdf_detects_heading_by_font_size_and_merges_paragraph_lines():
    data = FIXTURE.read_bytes()
    item = RawItem(
        uri=f"file://{FIXTURE}",
        content=data,
        content_hash=content_hash(data),
        modified_at=datetime.now(timezone.utc),
    )
    source_ref = SourceRef(source_id="s1", source_type=SourceType.LOCAL_FILE, uri=item.uri)

    document = PDFParser().parse(item, source_ref)

    assert document.metadata.page_count == 1

    headings = [e for e in document.elements if isinstance(e, Heading)]
    texts = [e for e in document.elements if isinstance(e, TextBlock)]

    assert len(headings) == 1
    assert headings[0].text == "Knowledge Forge Sample"
    assert headings[0].page == 1

    # The two normal-size lines are close together vertically and should
    # merge into a single paragraph text block.
    assert len(texts) == 1
    assert "short paragraph" in texts[0].text
    assert "two lines" in texts[0].text
    assert texts[0].page == 1
