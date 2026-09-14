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


class _BackfillStore:
    """`embed`'s view of PipelineStore: `missing` chunk ids come back from the
    missing-embedding query until recorded. Tracks in-flight writes to prove
    batches overlap, and every recorded id to prove none is embedded twice."""

    def __init__(self, missing: int):
        from knowledge_forge.models.chunk import Chunk, HierarchyLevel

        self.missing = [
            Chunk(chunk_id=f"c{i}", document_id="d", source_id="s", hierarchy_level=HierarchyLevel.PARAGRAPH,
                  sequence_index=i, text=f"t{i}", token_count=1)
            for i in range(missing)
        ]
        self.recorded: list[str] = []
        self.active = self.max_active = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        pass

    async def init_schema(self):
        pass

    async def list_chunks_missing_embedding(self, embedding_model, project_id=None, limit=50):
        return [(chunk, "p") for chunk in self.missing[:limit]]

    async def record_chunk_embeddings(self, chunks, project_id, embedder, embedding_model):
        import asyncio

        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        self.recorded += [c.chunk_id for c in chunks]
        self.missing = [c for c in self.missing if c not in chunks]


def test_embed_backfill_embeds_disjoint_batches_concurrently_and_each_row_once(monkeypatch):
    store = _BackfillStore(missing=7)
    monkeypatch.setattr(cli.settings, "database_url", "postgresql://unused")
    monkeypatch.setattr(cli.settings, "max_concurrent_llm_requests", 3)
    monkeypatch.setattr(cli, "PipelineStore", lambda *a, **k: store)
    monkeypatch.setattr(cli, "get_embedding_provider", lambda model=None: object())

    cli.embed(project_id=None, batch_size=2, model="m", embedding_source="chunk")

    assert sorted(store.recorded) == sorted(f"c{i}" for i in range(7))
    assert len(store.recorded) == len(set(store.recorded))
    assert store.max_active == 3
