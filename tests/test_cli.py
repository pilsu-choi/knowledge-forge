"""_build_collector() is the seam extract/schedule use to pick a Collector
via --source (wiki/roadmap.md 1단계) - "local" (default) needs PATH and
returns a LocalFileCollector, anything else delegates to
collectors/saas/factory.py's build_*_collector() and ignores PATH.
"""

from datetime import timezone

import pytest
import typer

from knowledge_forge import cli
from knowledge_forge.collectors.local_file import LocalFileCollector


def test_local_source_requires_path():
    with pytest.raises(typer.Exit):
        cli._build_collector("local", None)


def test_local_source_returns_local_file_collector(tmp_path):
    target = tmp_path / "doc.md"
    target.write_text("hello")

    collector = cli._build_collector("local", target)

    assert isinstance(collector, LocalFileCollector)


def test_unknown_source_exits():
    with pytest.raises(typer.Exit):
        cli._build_collector("carrier-pigeon", None)


def test_saas_source_delegates_to_matching_factory_builder(monkeypatch):
    sentinel = object()
    monkeypatch.setitem(cli.SAAS_COLLECTOR_BUILDERS, "slack", lambda: sentinel)

    assert cli._build_collector("slack", None) is sentinel


# _parse_datetime_arg() is `backfill`'s --since/--until parser (wiki/scheduling.md
# "놓친 구간 재수집") - naive input is treated as UTC (see collectors/base.py's
# ensure_utc) since every RawItem.modified_at it's compared against is UTC-aware.


def test_parse_datetime_arg_accepts_date_only_and_treats_it_as_utc_midnight():
    parsed = cli._parse_datetime_arg("2026-08-01", "--since")

    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0
    assert (parsed.year, parsed.month, parsed.day, parsed.hour) == (2026, 8, 1, 0)


def test_parse_datetime_arg_accepts_naive_datetime_and_normalizes_to_utc():
    parsed = cli._parse_datetime_arg("2026-08-01T09:30:00", "--since")

    assert parsed == parsed.replace(tzinfo=timezone.utc)
    assert parsed.hour == 9 and parsed.minute == 30


def test_parse_datetime_arg_preserves_explicit_offset():
    parsed = cli._parse_datetime_arg("2026-08-01T09:00:00+09:00", "--since")

    assert parsed.utcoffset().total_seconds() == 9 * 3600


def test_parse_datetime_arg_rejects_unparseable_value():
    with pytest.raises(typer.Exit):
        cli._parse_datetime_arg("not-a-date", "--since")
