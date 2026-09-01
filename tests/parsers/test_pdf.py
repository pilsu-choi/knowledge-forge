import io
from datetime import datetime, timezone
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.ids import content_hash
from knowledge_forge.models.document import Heading, TextBlock
from knowledge_forge.models.source import SourceRef, SourceType
from knowledge_forge.parsers.pdf import PDFParser

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sample.pdf"


async def test_pdf_detects_heading_by_font_size_and_merges_paragraph_lines():
    data = FIXTURE.read_bytes()
    item = RawItem(
        uri=f"file://{FIXTURE}",
        content=data,
        content_hash=content_hash(data),
        modified_at=datetime.now(timezone.utc),
    )
    source_ref = SourceRef(source_id="s1", source_type=SourceType.LOCAL_FILE, uri=item.uri)

    document = await PDFParser().parse(item, source_ref)

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


async def test_pdf_captures_indent_level_from_line_x_position():
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setFont("Helvetica", 11)
    # Two lines close together at the page's leftmost x0 - one paragraph,
    # no indent.
    c.drawString(72, 720, "alpha line one")
    c.drawString(72, 705, "alpha line two")
    # A big vertical gap starts a new paragraph, offset well to the right -
    # indented. The second line of this paragraph stays close to the first
    # so it merges into the same block (indent comes from the first line).
    c.drawString(112, 600, "beta line one")
    c.drawString(112, 585, "beta line two")
    c.save()
    data = buf.getvalue()

    item = RawItem(
        uri="file://in-memory.pdf",
        content=data,
        content_hash=content_hash(data),
        modified_at=datetime.now(timezone.utc),
    )
    source_ref = SourceRef(source_id="s1", source_type=SourceType.LOCAL_FILE, uri=item.uri)

    document = await PDFParser().parse(item, source_ref)
    texts = [e for e in document.elements if isinstance(e, TextBlock)]

    assert len(texts) == 2
    assert "alpha line one" in texts[0].text
    assert texts[0].indent_level is None
    assert "beta line one" in texts[1].text
    assert texts[1].indent_level == 2
