from datetime import datetime, timezone
from pathlib import Path

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.ids import content_hash
from knowledge_forge.models.document import TableElement, TextBlock
from knowledge_forge.models.source import SourceRef, SourceType
from knowledge_forge.parsers.xlsx import XlsxParser, _build_structured

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sample_regions.xlsx"


async def _parse():
    data = FIXTURE.read_bytes()
    item = RawItem(
        uri=f"file://{FIXTURE}",
        content=data,
        content_hash=content_hash(data),
        modified_at=datetime.now(timezone.utc),
    )
    source_ref = SourceRef(source_id="s1", source_type=SourceType.LOCAL_FILE, uri=item.uri)
    return await XlsxParser().parse(item, source_ref)


async def test_sheet_splits_into_title_table_and_note_regions():
    document = await _parse()

    assert [type(e) for e in document.elements] == [TextBlock, TableElement, TextBlock]
    title, table, note = document.elements

    assert title.text == "진료비 기준표"
    assert note.text == "※ 야간 진료는 별도 가산됨"
    assert isinstance(table, TableElement)
    assert table.caption == "Report"


async def test_merged_header_is_propagated_and_detected():
    document = await _parse()
    table = next(e for e in document.elements if isinstance(e, TableElement))

    assert table.header_rows == 2
    # merged "2025년" (B3:C3) propagates into C3, instead of coming back blank.
    assert table.rows[0] == ["구분", "2025년", "2025년"]
    assert table.rows[1] == ["", "기본급", "가산율"]
    assert table.rows[2] == ["외래", "10000", "20%"]
    assert table.rows[3] == ["입원", "20000", "30%"]
    # B7's formula has no cached value in an openpyxl-generated file - blank
    # is the expected, correct read here (see generate_regions_fixture).
    assert table.rows[4] == ["합계", "", ""]


async def test_structured_view_nests_columns_under_the_merged_header():
    document = await _parse()
    table = next(e for e in document.elements if isinstance(e, TableElement))

    assert table.structured == {
        "headers": table.rows[:2],
        "rows": [
            {"항목": "외래", "2025년": {"기본급": "10000", "가산율": "20%"}},
            {"항목": "입원", "2025년": {"기본급": "20000", "가산율": "30%"}},
            {"항목": "합계", "2025년": {"기본급": "", "가산율": ""}},
        ],
    }


async def test_formula_string_is_recovered_despite_missing_cached_value():
    document = await _parse()
    table = next(e for e in document.elements if isinstance(e, TableElement))

    assert table.formulas == {"B7": "=B5+B6"}


def test_structured_view_flattens_a_column_whose_header_path_collides():
    """A ragged header where one column is headed only "PM53" and the next
    is headed "PM53" over "실적": the shorter path claims "PM53" for a
    string, so the longer one cannot nest under it. It flattens instead of
    raising - which used to abort the whole workbook."""
    headers = [["구분", "PM53", "PM53"], ["", "", "실적"]]
    rows = [["2021", "100", "90"]]

    structured = _build_structured(headers, rows)

    # The record's own key comes from the *last* header row's first cell,
    # which is blank here - hence the "항목" default.
    assert structured["rows"] == [{"항목": "2021", "PM53": "100", "PM53/실적": "90"}]
