"""Integration tests against a real PostgreSQL instance.

Skipped unless KF_TEST_DATABASE_URL is set - same pattern as
tests/storage/test_job_store.py. To run these locally:

    docker compose up -d postgres
    KF_TEST_DATABASE_URL=postgresql://kf:kf@localhost:15432/knowledge_forge uv run pytest tests/storage/test_eval_store.py -v
"""

import os

import pytest

from knowledge_forge.storage.eval_store import EvalStore

DSN = os.environ.get("KF_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DSN, reason="KF_TEST_DATABASE_URL not set - no Postgres to test against")


@pytest.fixture(autouse=True)
async def _clear_tables():
    # Shared dev/CI Postgres, not a per-test sandbox - clearing before each
    # test makes these independent of whatever else touched these tables.
    async with EvalStore(DSN) as store:
        await store.init_schema()
        await store.conn.execute("DELETE FROM eval_score")
        await store.conn.execute("DELETE FROM search_feedback")
        await store.conn.execute("DELETE FROM search_log")
    yield


async def test_log_search_and_get_roundtrip():
    async with EvalStore(DSN) as store:
        log = await store.log_search(
            query="지난달 결정된 사항은?",
            answer="A, B 결정이 있었습니다.",
            evidence=[{"tool": "file_search", "iteration": 1, "payload": [{"path": "a.md"}]}],
            tool_calls=[{"tool": "file_search", "arguments": {"query": "결정"}, "iteration": 1}],
            latency_ms=1234,
        )

        fetched = await store.get_search_log(log.log_id)
        assert fetched is not None
        assert fetched.query == "지난달 결정된 사항은?"
        assert fetched.project_id == "default"
        assert fetched.latency_ms == 1234
        assert fetched.evidence[0]["tool"] == "file_search"


async def test_get_search_log_missing_returns_none():
    async with EvalStore(DSN) as store:
        assert await store.get_search_log("does-not-exist") is None


async def test_list_search_logs_filters_by_project_id():
    async with EvalStore(DSN) as store:
        log_default = await store.log_search("q1", "a1", [], [], 10)
        log_a = await store.log_search("q2", "a2", [], [], 10, project_id="proj-a")

        proj_a_logs = await store.list_search_logs(project_id="proj-a", limit=200)
        assert {log.log_id for log in proj_a_logs} == {log_a.log_id}

        all_logs = await store.list_search_logs(limit=200)
        assert {log.log_id for log in all_logs} >= {log_default.log_id, log_a.log_id}


async def test_add_feedback():
    async with EvalStore(DSN) as store:
        log = await store.log_search("q", "a", [], [], 10)
        feedback = await store.add_feedback(log.log_id, "up", "정확했음")

        assert feedback.log_id == log.log_id
        assert feedback.rating == "up"
        assert feedback.comment == "정확했음"


async def test_list_unscored_search_logs_excludes_scored():
    async with EvalStore(DSN) as store:
        unscored = await store.log_search("q1", "a1", [], [], 10)
        scored = await store.log_search("q2", "a2", [], [], 10)
        await store.save_eval_score(scored.log_id, 0.5, None, [], "test-model")

        pending = await store.list_unscored_search_logs(limit=200)
        pending_ids = {log.log_id for log in pending}
        assert unscored.log_id in pending_ids
        assert scored.log_id not in pending_ids


async def test_save_eval_score_upserts():
    async with EvalStore(DSN) as store:
        log = await store.log_search("q", "a", [], [], 10)
        await store.save_eval_score(log.log_id, 0.5, 0.5, [{"text": "x", "verdict": "unsupported"}], "model-v1")
        updated = await store.save_eval_score(log.log_id, 1.0, 1.0, [], "model-v2")

        fetched = await store.get_eval_score(log.log_id)
        assert fetched.groundedness_score == 1.0
        assert fetched.judge_model == "model-v2"
        assert fetched.claims == []
        assert updated.log_id == log.log_id
