from datetime import datetime, timezone
from pathlib import Path

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.ids import content_hash
from knowledge_forge.models.document import TableElement
from knowledge_forge.models.source import SourceRef, SourceType
from knowledge_forge.parsers.xlsx import XlsxParser

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sample.xlsx"


def test_xlsx_parses_sheet_into_a_table_element():
    data = FIXTURE.read_bytes()
    item = RawItem(
        uri=f"file://{FIXTURE}",
        content=data,
        content_hash=content_hash(data),
        modified_at=datetime.now(timezone.utc),
    )
    source_ref = SourceRef(source_id="s1", source_type=SourceType.LOCAL_FILE, uri=item.uri)

    document = XlsxParser().parse(item, source_ref)

    tables = [e for e in document.elements if isinstance(e, TableElement)]
    assert len(tables) == 1
    assert tables[0].caption == "Sheet1"
    assert tables[0].rows[0] == ["id", "name", "amount"]
    assert tables[0].rows[1] == ["1", "Ada", "100"]
    assert tables[0].rows[2] == ["2", "Grace", "200"]

    assert all(e.page is None for e in document.elements)
