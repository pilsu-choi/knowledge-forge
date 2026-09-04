import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from knowledge_forge.search.sql import (
    DEFAULT_MAX_ROWS,
    SqlSearchError,
    _json_safe,
    _validate_readonly_sql,
    describe_schema,
    sql_search,
)


class _FakeStore:
    def __init__(self, rows, columns):
        self._rows = rows
        self._columns = columns
        self.calls: list[tuple] = []

    async def execute_readonly_query(self, sql, limit):
        self.calls.append((sql, limit))
        return self._rows, self._columns


# --- _validate_readonly_sql -------------------------------------------------


def test_validate_readonly_sql_accepts_a_plain_select():
    assert _validate_readonly_sql("SELECT * FROM knowledge_objects") == "SELECT * FROM knowledge_objects"


def test_validate_readonly_sql_accepts_a_with_query():
    sql = "WITH x AS (SELECT 1) SELECT * FROM x"
    assert _validate_readonly_sql(sql) == sql


def test_validate_readonly_sql_strips_a_single_trailing_semicolon():
    assert _validate_readonly_sql("SELECT 1;") == "SELECT 1"


def test_validate_readonly_sql_is_case_insensitive():
    assert _validate_readonly_sql("select 1") == "select 1"


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO knowledge_objects VALUES (1)",
        "UPDATE knowledge_objects SET summary = 'x'",
        "DELETE FROM knowledge_objects",
        "DROP TABLE knowledge_objects",
        "ALTER TABLE knowledge_objects ADD COLUMN x TEXT",
        "TRUNCATE knowledge_objects",
        "CREATE TABLE x (id TEXT)",
        "GRANT ALL ON knowledge_objects TO public",
        "CALL some_procedure()",
        "COPY knowledge_objects TO '/tmp/x'",
    ],
)
def test_validate_readonly_sql_rejects_write_and_ddl_statements(sql):
    with pytest.raises(SqlSearchError):
        _validate_readonly_sql(sql)


def test_validate_readonly_sql_rejects_statements_not_starting_with_select_or_with():
    with pytest.raises(SqlSearchError):
        _validate_readonly_sql("SHOW search_path")


def test_validate_readonly_sql_rejects_stacked_statements():
    with pytest.raises(SqlSearchError):
        _validate_readonly_sql("SELECT 1; DROP TABLE knowledge_objects")


def test_validate_readonly_sql_rejects_a_write_keyword_hidden_inside_a_select():
    # A model could try to smuggle a write via a CTE or subquery - the
    # keyword check looks at the whole statement, not just its leading verb.
    with pytest.raises(SqlSearchError):
        _validate_readonly_sql("SELECT * FROM knowledge_objects; INSERT INTO knowledge_objects VALUES (1)")


# --- _json_safe --------------------------------------------------------


def test_json_safe_passes_through_plain_types():
    assert _json_safe("x") == "x"
    assert _json_safe(1) == 1
    assert _json_safe(1.5) == 1.5
    assert _json_safe(True) is True
    assert _json_safe(None) is None


def test_json_safe_converts_datetime_and_date_to_isoformat():
    dt = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    assert _json_safe(dt) == dt.isoformat()
    d = date(2026, 9, 4)
    assert _json_safe(d) == d.isoformat()


def test_json_safe_converts_decimal_to_float():
    assert _json_safe(Decimal("1.5")) == 1.5


def test_json_safe_converts_uuid_to_str():
    u = uuid.uuid4()
    assert _json_safe(u) == str(u)


def test_json_safe_omits_long_bytes():
    result = _json_safe(b"x" * 100)
    assert "100 bytes" in result


def test_json_safe_omits_a_long_numeric_list_as_a_vector():
    result = _json_safe([0.1] * 1536)
    assert result == "<vector, 1536 dims, omitted>"


def test_json_safe_keeps_a_short_numeric_list():
    assert _json_safe([1, 2, 3]) == [1, 2, 3]


def test_json_safe_recurses_into_dicts_and_lists():
    dt = datetime(2026, 9, 4, tzinfo=timezone.utc)
    value = {"a": [dt, {"b": Decimal("2")}]}
    assert _json_safe(value) == {"a": [dt.isoformat(), {"b": 2.0}]}


def test_json_safe_falls_back_to_str_for_unknown_types():
    class Weird:
        def __str__(self):
            return "weird"

    assert _json_safe(Weird()) == "weird"


# --- describe_schema -----------------------------------------------------


def test_describe_schema_fills_in_the_row_limit():
    text = describe_schema(max_rows=42)
    assert "42행" in text
    assert "__MAX_ROWS__" not in text


def test_describe_schema_default_uses_default_max_rows():
    assert str(DEFAULT_MAX_ROWS) in describe_schema()


# --- sql_search ------------------------------------------------------------


async def test_sql_search_returns_columns_rows_and_row_count():
    store = _FakeStore(rows=[("ko1", "decision")], columns=["knowledge_object_id", "type"])

    result = await sql_search("SELECT knowledge_object_id, type FROM knowledge_objects", store)

    assert result == {
        "columns": ["knowledge_object_id", "type"],
        "rows": [{"knowledge_object_id": "ko1", "type": "decision"}],
        "row_count": 1,
        "truncated": False,
    }
    assert store.calls == [("SELECT knowledge_object_id, type FROM knowledge_objects", DEFAULT_MAX_ROWS)]


async def test_sql_search_marks_truncated_when_row_count_hits_the_limit():
    store = _FakeStore(rows=[("a",), ("b",)], columns=["x"])

    result = await sql_search("SELECT x FROM t", store, limit=2)

    assert result["truncated"] is True


async def test_sql_search_json_safes_row_values():
    dt = datetime(2026, 9, 4, tzinfo=timezone.utc)
    store = _FakeStore(rows=[(dt,)], columns=["extracted_at"])

    result = await sql_search("SELECT extracted_at FROM knowledge_objects", store)

    assert result["rows"] == [{"extracted_at": dt.isoformat()}]


async def test_sql_search_rejects_before_touching_the_store():
    store = _FakeStore(rows=[], columns=[])

    with pytest.raises(SqlSearchError):
        await sql_search("DELETE FROM knowledge_objects", store)

    assert store.calls == []
