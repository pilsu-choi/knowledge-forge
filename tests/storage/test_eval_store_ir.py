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

from knowledge_forge.evaluation.ir_runner import corpus_identity, embedding_consistency, snapshot_drift
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
        # 임베딩 -> 청크 -> 문서 순으로(외래키). 스냅샷 테스트가 청크/임베딩까지 만든다.
        await store.conn.execute(
            "DELETE FROM knowledge_object_embeddings WHERE document_id LIKE %s", (PREFIX + "%",)
        )
        await store.conn.execute("DELETE FROM chunks WHERE document_id LIKE %s", (PREFIX + "%",))
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


async def test_dirty_files_roundtrip_and_distinguishes_same_commit_runs():
    """같은 커밋에서 잰 두 run이 표 위에서 구분돼야 한다 - b470057 하나에
    ndcg@10 0.736(ablate-vector)과 0.477(purity-vector-only)이 같은 git_commit으로
    적혔던 사고가 이 컬럼과 `+dirty.` 표기의 이유다."""
    sha = "b470057d93c004101fc983be26d8c06e050e47de"
    clean_id, dirty_id = _rid(), _rid()
    async with EvalStore(DSN) as store:
        await store.save_eval_run(_run(clean_id, git_commit=sha, label="ablate-vector"))
        await store.save_eval_run(
            _run(
                dirty_id,
                git_commit=f"{sha}+dirty.0123456789ab",
                dirty_files=[" M src/knowledge_forge/search/retrieve.py"],
                label="purity-vector-only",
            )
        )

        clean = await store.get_eval_run(clean_id)
        dirty = await store.get_eval_run(dirty_id)
        assert clean is not None and dirty is not None
        assert clean.git_commit != dirty.git_commit
        assert clean.dirty_files is None  # 깨끗한 run은 NULL
        assert dirty.dirty_files == [" M src/knowledge_forge/search/retrieve.py"]
        # 커밋이 접두사로 남아 있어 예전 조회 방식은 그대로 산다.
        async with store.conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM eval_run WHERE git_commit LIKE %s AND run_id LIKE %s",
                (sha + "%", PREFIX + "%"),
            )
            row = await cur.fetchone()
        assert row is not None and row[0] == 2


async def test_pre_migration_rows_survive_as_null():
    """dirty_files 이전에 쌓인 행(dev DB에 60개 이상)은 그대로 읽혀야 한다.
    컬럼을 모르는 INSERT = 마이그레이션 이전 행."""
    run_id = _rid()
    async with EvalStore(DSN) as store:
        await store.conn.execute(
            """
            INSERT INTO eval_run (run_id, golden_version, golden_path, config, config_hash, index_snapshot,
                                  git_commit, label, started_at, summary)
            VALUES (%s, 'vtest', 'v.json', %s, 'cfg', %s, 'deadbeef', 'legacy', %s, %s)
            """,
            (run_id, Jsonb({}), Jsonb({}), datetime(2026, 9, 4, tzinfo=timezone.utc), Jsonb({"overall": {"n": 1}})),
        )
        fetched = await store.get_eval_run(run_id)
        assert fetched is not None
        assert fetched.git_commit == "deadbeef"
        assert fetched.dirty_files is None
        assert fetched.summary == {"overall": {"n": 1}}


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
        assert set(before) == {
            "documents",
            "chunks",
            "knowledge_objects",
            "embeddings_by_kind",
            "embeddings_by_model_and_kind",
            "fingerprints",
        }
        assert isinstance(before["embeddings_by_kind"], dict)
        empty = await store.snapshot_index(project)
        assert empty["documents"] == 0 and empty["embeddings_by_model_and_kind"] == {}
        # 빈 프로젝트는 지문도 전부 None(string_agg가 NULL) - "지문이 없다"와
        # "지문이 같다"가 섞이지 않게 키 자체는 남는다.
        assert set(empty["fingerprints"]) == {"document_titles", "chunk_text", "embeddings", "source_content"}
        assert set(empty["fingerprints"].values()) == {None}

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


async def test_snapshot_counts_embeddings_per_model_not_only_per_grain():
    """모델별로 쪼개 세는 게 핵심이다 - 검색은 `WHERE embedding_model = %s`로 거르므로
    (`search/service.py::vector_search`) 다른 모델의 벡터는 이번 run의 벡터 arm에
    존재하지 않는 것과 같다. grain 합계만 보면 "청크 2개 / 임베딩 2개 = 정합"이지만
    실제로는 활성 모델 벡터가 1개뿐인 상황을 구분해야 한다."""
    source_id = PREFIX + "src-" + uuid.uuid4().hex[:8]
    doc_id = PREFIX + "doc-" + uuid.uuid4().hex[:8]
    project = PREFIX + "proj-emb-" + uuid.uuid4().hex[:6]
    vector = "[" + ",".join(["0"] * 1536) + "]"
    async with EvalStore(DSN) as store:
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
        chunk_ids = [PREFIX + "chunk-" + uuid.uuid4().hex[:8] for _ in range(2)]
        for i, chunk_id in enumerate(chunk_ids):
            await store.conn.execute(
                """
                INSERT INTO chunks (chunk_id, document_id, source_id, hierarchy_level, sequence_index, text, token_count)
                VALUES (%s, %s, %s, 'paragraph', %s, 'text', 100)
                """,
                (chunk_id, doc_id, source_id, i),
            )
        # 청크 2개, 임베딩 2개 - 그러나 모델이 서로 다르다.
        for chunk_id, model in zip(chunk_ids, ["model-a", "model-b"], strict=True):
            await store.conn.execute(
                """
                INSERT INTO knowledge_object_embeddings
                    (embedding_id, target_kind, chunk_id, document_id, embedding_model, embedding,
                     embedded_text, embedded_text_hash, project_id, created_at)
                VALUES (%s, 'chunk', %s, %s, %s, %s, 'text', 'h', %s, %s)
                """,
                (uuid.uuid4().hex, chunk_id, doc_id, model, vector, project, datetime.now(timezone.utc)),
            )

        snapshot = await store.snapshot_index(project)
        assert snapshot["chunks"] == 2
        assert snapshot["embeddings_by_kind"] == {"chunk": 2}  # 합계만 보면 "정합"
        assert snapshot["embeddings_by_model_and_kind"] == {"model-a": {"chunk": 1}, "model-b": {"chunk": 1}}

        consistency = embedding_consistency(snapshot, "model-a")
        assert consistency["status"] == "mismatch"
        assert consistency["grains"]["chunk"] == {"total": 2, "embedded": 1, "coverage": 0.5}
        assert consistency["other_models"] == {"model-b": 1}


async def test_title_rewrite_changes_the_fingerprint_though_no_count_moves():
    """이 지문이 존재하는 이유. 2026-09-05에 다른 세션이 슬랙 문서 197건의
    `documents.title`을 다시 쓰자 같은 코드의 v0가 0.8516 -> 0.8490으로 움직였는데,
    행 수도 `updated_at`도 `sources.content_hash`도 그대로였다 - 행 수 스냅샷과
    `check_staleness` 어느 쪽도 못 잡는 변조였다. 제목은 pg_trgm 입력이라 랭킹을
    실제로 바꾼다."""
    source_id = PREFIX + "src-" + uuid.uuid4().hex[:8]
    doc_id = PREFIX + "doc-" + uuid.uuid4().hex[:8]
    project = PREFIX + "proj-fp-" + uuid.uuid4().hex[:6]
    async with EvalStore(DSN) as store:
        await store.conn.execute(
            """
            INSERT INTO sources (source_id, source_type, uri, content_hash, collected_at, metadata, project_id)
            VALUES (%s, 'slack', %s, 'hash-1', %s, %s, %s)
            """,
            (source_id, f"slack:///{source_id}", datetime.now(timezone.utc), Jsonb({}), project),
        )
        await store.conn.execute(
            """
            INSERT INTO documents (document_id, source_id, elements, metadata, project_id, title)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (doc_id, source_id, Jsonb([]), Jsonb({}), project, "#C0BSW8Y2AD9 2026-09-02"),
        )
        before = await store.snapshot_index(project)

        # 제목만 다시 쓴다 - 행 수도, sources.content_hash도 그대로.
        await store.conn.execute(
            "UPDATE documents SET title = %s WHERE document_id = %s",
            ("#proj_2026-038-0_nh농협은행_mvp 2026-09-02", doc_id),
        )
        after = await store.snapshot_index(project)

    assert corpus_identity(before) != corpus_identity(after)
    drift = snapshot_drift(before, after)
    assert list(drift) == ["fingerprints.document_titles"]
    # 행 수 기반 키는 하나도 안 움직였다 - 그래서 지문이 필요했다.
    assert before["documents"] == after["documents"] == 1
    assert before["fingerprints"]["source_content"] == after["fingerprints"]["source_content"]
