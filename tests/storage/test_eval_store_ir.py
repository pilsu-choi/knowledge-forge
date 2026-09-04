"""`EvalStore`'s eval_run / eval_run_query half against a real PostgreSQL.

Skipped unless KF_TEST_DATABASE_URL is set (same pattern as
tests/storage/test_eval_store.py). Unlike that file this one does *not*
truncate tables: the dev DB's eval_run history is real data worth keeping,
so every row written here carries a `test-ir-` prefix and is deleted in
teardown.

    KF_TEST_DATABASE_URL=postgresql://kf:kf@localhost:15432/knowledge_forge uv run pytest tests/storage/test_eval_store_ir.py -v
"""

import os
import uuid
from datetime import datetime, timezone

import pytest
from psycopg.types.json import Jsonb

from knowledge_forge.storage.eval_store import EvalRun, EvalRunQuery, EvalStore

DSN = os.environ.get("KF_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DSN, reason="KF_TEST_DATABASE_URL not set - no Postgres to test against")

PREFIX = "test-ir-"


def _run(run_id: str, **overrides) -> EvalRun:
    base = dict(
        run_id=run_id,
        golden_version="vtest",
        golden_path="tests/eval/golden/vtest.json",
        project_id=None,
        config={"limit": 10, "k_values": [1, 3]},
        config_hash="abc123abc123",
        index_snapshot={"documents": 1},
        git_commit="deadbeef",
        label="unit",
        started_at=datetime(2026, 9, 4, 1, 0, tzinfo=timezone.utc),
        finished_at=None,
        summary={},
    )
    base.update(overrides)
    return EvalRun(**base)


@pytest.fixture(autouse=True)
async def _cleanup():
    async with EvalStore(DSN) as store:
        await store.init_schema()
    yield
    async with EvalStore(DSN) as store:
        # eval_run_query cascades from eval_run.
        await store.conn.execute("DELETE FROM eval_run WHERE run_id LIKE %s", (PREFIX + "%",))
        await store.conn.execute("DELETE FROM documents WHERE document_id LIKE %s", (PREFIX + "%",))
        await store.conn.execute("DELETE FROM sources WHERE source_id LIKE %s", (PREFIX + "%",))


def _rid() -> str:
    return PREFIX + uuid.uuid4().hex[:12]


async def test_save_run_twice_upserts_and_roundtrips():
    run_id = _rid()
    async with EvalStore(DSN) as store:
        first = await store.save_eval_run(_run(run_id))
        assert first.finished_at is None
        assert first.summary == {}

        finished = _run(
            run_id,
            finished_at=datetime(2026, 9, 4, 1, 5, tzinfo=timezone.utc),
            summary={"overall": {"n": 2, "mrr": 0.75}},
        )
        second = await store.save_eval_run(finished)
        assert second.finished_at is not None
        assert second.summary["overall"]["mrr"] == 0.75

        fetched = await store.get_eval_run(run_id)
        assert fetched is not None
        assert fetched.golden_version == "vtest"
        assert fetched.config == {"limit": 10, "k_values": [1, 3]}
        assert fetched.index_snapshot == {"documents": 1}
        assert fetched.git_commit == "deadbeef"
        assert fetched.label == "unit"
        assert fetched.queries == []  # never populated by the store
        assert await store.get_eval_run(PREFIX + "missing") is None


async def test_run_queries_roundtrip_and_cascade():
    run_id = _rid()
    rows = [
        EvalRunQuery(
            run_id=run_id,
            query_id="q-b",
            query_text="b?",
            primary_type="factoid",
            types=["factoid"],
            ranked_document_ids=["d1", "d2"],
            scores=[0.9, 0.1],
            metrics={"mrr": 1.0, "hit@1": 1.0},
            strategies=["vector"],
            steps=[{"strategy": "vector", "n_hits": 2}],
            elapsed_ms=3.5,
        ),
        EvalRunQuery(
            run_id=run_id,
            query_id="q-a",
            query_text="a?",
            primary_type="aggregation",
            types=["aggregation"],
            predicted_count=4,
            error="RuntimeError: boom",
        ),
    ]
    async with EvalStore(DSN) as store:
        await store.save_eval_run(_run(run_id))
        assert await store.save_eval_run_queries(run_id, rows) == 2
        assert await store.save_eval_run_queries(run_id, []) == 0

        fetched = await store.list_eval_run_queries(run_id)
        assert [q.query_id for q in fetched] == ["q-a", "q-b"]  # ordered by query_id
        b = fetched[1]
        assert b.ranked_document_ids == ["d1", "d2"]
        assert b.scores == [0.9, 0.1]
        assert b.metrics == {"mrr": 1.0, "hit@1": 1.0}
        assert b.steps == [{"strategy": "vector", "n_hits": 2}]
        assert b.elapsed_ms == 3.5
        assert b.error is None
        a = fetched[0]
        assert a.predicted_count == 4
        assert a.error == "RuntimeError: boom"
        assert a.metrics == {}

        # Re-saving the same (run_id, query_id) updates rather than duplicating.
        rows[0].metrics = {"mrr": 0.5}
        await store.save_eval_run_queries(run_id, rows[:1])
        assert (await store.list_eval_run_queries(run_id))[1].metrics == {"mrr": 0.5}

        await store.conn.execute("DELETE FROM eval_run WHERE run_id = %s", (run_id,))
        assert await store.list_eval_run_queries(run_id) == []


async def test_list_runs_filters_by_golden_version_newest_first():
    older, newer, other = _rid(), _rid(), _rid()
    async with EvalStore(DSN) as store:
        await store.save_eval_run(_run(older, started_at=datetime(2026, 9, 4, 1, 0, tzinfo=timezone.utc)))
        await store.save_eval_run(_run(newer, started_at=datetime(2026, 9, 4, 2, 0, tzinfo=timezone.utc)))
        await store.save_eval_run(_run(other, golden_version="vtest-other"))

        runs = [r for r in await store.list_eval_runs(golden_version="vtest", limit=100) if r.run_id.startswith(PREFIX)]
        assert [r.run_id for r in runs] == [newer, older]
        assert [r.run_id for r in await store.list_eval_runs(golden_version="vtest-other")] == [other]
        assert await store.list_eval_runs(golden_version="vtest-nope") == []


async def test_snapshot_index_and_content_hashes_for():
    source_id = PREFIX + "src-" + uuid.uuid4().hex[:8]
    doc_id = PREFIX + "doc-" + uuid.uuid4().hex[:8]
    project = PREFIX + "proj"
    async with EvalStore(DSN) as store:
        before = await store.snapshot_index()
        assert set(before) == {"documents", "chunks", "knowledge_objects", "embeddings_by_kind"}
        assert isinstance(before["embeddings_by_kind"], dict)
        assert await store.snapshot_index(project) == {
            "documents": 0,
            "chunks": 0,
            "knowledge_objects": 0,
            "embeddings_by_kind": {},
        }

        await store.conn.execute(
            """
            INSERT INTO sources (source_id, source_type, uri, content_hash, collected_at, metadata, project_id)
            VALUES (%s, 'local_file', %s, 'hash-1', %s, %s, %s)
            """,
            (source_id, f"file:///{source_id}", datetime.now(timezone.utc), Jsonb({}), project),
        )
        await store.conn.execute(
            "INSERT INTO documents (document_id, source_id, elements, metadata, project_id) VALUES (%s, %s, %s, %s, %s)",
            (doc_id, source_id, Jsonb([]), Jsonb({}), project),
        )

        scoped = await store.snapshot_index(project)
        assert scoped["documents"] == 1
        assert scoped["chunks"] == 0
        assert (await store.snapshot_index())["documents"] == before["documents"] + 1

        hashes = await store.content_hashes_for([doc_id, PREFIX + "does-not-exist"])
        assert hashes == {doc_id: "hash-1"}
        assert await store.content_hashes_for([]) == {}
