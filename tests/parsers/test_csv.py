from datetime import datetime, timezone
from pathlib import Path

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.ids import content_hash
from knowledge_forge.models.document import TableElement
from knowledge_forge.models.source import SourceRef, SourceType
from knowledge_forge.parsers.csv import CsvParser

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sample.csv"


def _item(data: bytes) -> RawItem:
    return RawItem(
        uri=f"file://{FIXTURE}",
        content=data,
        content_hash=content_hash(data),
        modified_at=datetime.now(timezone.utc),
    )


def _source_ref() -> SourceRef:
    return SourceRef(source_id="s1", source_type=SourceType.LOCAL_FILE, uri=f"file://{FIXTURE}")


async def test_csv_parses_into_a_single_table_element():
    document = await CsvParser().parse(_item(FIXTURE.read_bytes()), _source_ref())

    assert len(document.elements) == 1
    table = document.elements[0]
    assert isinstance(table, TableElement)
    assert table.header_rows == 1
    assert table.rows == [
        ["id", "name", "amount"],
        ["1", "Ada", "100"],
        ["2", "Grace", "200"],
    ]
    assert table.page is None


async def test_csv_skips_fully_blank_rows():
    data = b"id,name\n1,Ada\n\n2,Grace\n"

    document = await CsvParser().parse(_item(data), _source_ref())

    table = document.elements[0]
    assert table.rows == [["id", "name"], ["1", "Ada"], ["2", "Grace"]]


async def test_csv_decodes_cp949_when_not_utf8():
    data = "id,name\n1,가나다\n".encode("cp949")

    document = await CsvParser().parse(_item(data), _source_ref())

    table = document.elements[0]
    assert table.rows == [["id", "name"], ["1", "가나다"]]


async def test_csv_strips_utf8_bom():
    data = "id,name\n1,Ada\n".encode("utf-8-sig")

    document = await CsvParser().parse(_item(data), _source_ref())

    table = document.elements[0]
    assert table.rows[0] == ["id", "name"]
