"""`EvalStore`의 `eval-gen` 쪽 읽기 메서드(`list_chunk_candidates`/
`sources_by_type`)를 실제 PostgreSQL에 대고 확인한다.

`test_eval_store_ir.py`와 같은 규칙: KF_TEST_DATABASE_URL이 없으면 스킵하고,
개발 DB의 기존 데이터를 지우지 않기 위해 여기서 만든 행에만 `test-gen-` 접두사를
붙여 teardown에서 지운다. 그래서 단언도 "전체 개수"가 아니라 **우리가 만든
project_id 안에서**만 한다.

    KF_TEST_DATABASE_URL=postgresql://kf:kf@localhost:15432/knowledge_forge uv run pytest tests/storage/test_eval_store_gen.py -v
"""

import os
import uuid
from datetime import datetime, timezone

import pytest
from psycopg.types.json import Jsonb

from knowledge_forge.storage.eval_store import EvalStore

DSN = os.environ.get("KF_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DSN, reason="KF_TEST_DATABASE_URL not set - no Postgres to test against")

PREFIX = "test-gen-"


@pytest.fixture
async def seeded():
    """소스 타입 2종 × 문서 2개 × 청크(긴 것/짧은 것)를 한 project_id 안에 심는다."""
    project = PREFIX + uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc)
    rows = [
        # (source_type, doc suffix, [(chunk suffix, token_count)])
        ("slack", "a", [("long", 200), ("short", 5)]),
        ("gmail", "b", [("long", 150)]),
    ]
    async with EvalStore(DSN) as store:
        await store.init_schema()
        for source_type, suffix, chunks in rows:
            source_id = f"{PREFIX}src-{suffix}-{project}"
            document_id = f"{PREFIX}doc-{suffix}-{project}"
            await store.conn.execute(
                """
                INSERT INTO sources (source_id, source_type, uri, content_hash, collected_at, metadata, project_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (source_id, source_type, f"kf://{source_id}", f"hash-{suffix}", now, Jsonb({}), project),
            )
            await store.conn.execute(
                """
                INSERT INTO documents (document_id, source_id, elements, metadata, project_id)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (document_id, source_id, Jsonb([]), Jsonb({"title": f"제목 {suffix}"}), project),
            )
            for i, (chunk_suffix, token_count) in enumerate(chunks):
                await store.conn.execute(
                    """
                    INSERT INTO chunks (chunk_id, document_id, source_id, hierarchy_level, sequence_index,
                                        text, token_count)
                    VALUES (%s, %s, %s, 'section', %s, %s, %s)
                    """,
                    (
                        f"{PREFIX}chunk-{suffix}-{chunk_suffix}-{project}",
                        document_id,
                        source_id,
                        i,
                        f"본문 {suffix} {chunk_suffix}",
                        token_count,
                    ),
                )
    yield project
    async with EvalStore(DSN) as store:
        await store.conn.execute("DELETE FROM chunks WHERE chunk_id LIKE %s", (PREFIX + "%",))
        await store.conn.execute("DELETE FROM documents WHERE document_id LIKE %s", (PREFIX + "%",))
        await store.conn.execute("DELETE FROM sources WHERE source_id LIKE %s", (PREFIX + "%",))


async def test_list_chunk_candidates_filters_noise_and_joins_source_metadata(seeded):
    """토큰 하한이 짧은 청크(목차/서명/빈 슬라이드)를 잘라내고, 층화·정답 구성에
    필요한 값(source_type, content_hash, 문서 제목)이 조인돼 나와야 한다."""
    async with EvalStore(DSN) as store:
        candidates = await store.list_chunk_candidates(project_id=seeded, min_token_count=100)

    assert len(candidates) == 2 and all("-long-" in c.chunk_id for c in candidates)
    assert {c.source_type for c in candidates} == {"slack", "gmail"}
    assert all(c.content_hash.startswith("hash-") for c in candidates)
    assert all(c.title and c.title.startswith("제목") for c in candidates)
    assert all(c.uri.startswith("kf://") for c in candidates)


async def test_list_chunk_candidates_is_ordered_and_filterable_by_source_type(seeded):
    """순서가 `chunk_id` 기준으로 고정돼야 `--seed`로 같은 샘플이 재현된다 -
    DB의 기본 행 순서에 의존하면 시드가 의미를 잃는다."""
    async with EvalStore(DSN) as store:
        every = await store.list_chunk_candidates(project_id=seeded, min_token_count=0)
        slack_only = await store.list_chunk_candidates(
            project_id=seeded, min_token_count=0, source_types=["slack"]
        )

    assert [c.chunk_id for c in every] == sorted(c.chunk_id for c in every)
    assert len(every) == 3
    assert {c.source_type for c in slack_only} == {"slack"}


async def test_sources_by_type_is_scoped_by_project(seeded):
    async with EvalStore(DSN) as store:
        scoped = await store.sources_by_type(seeded)
        everything = await store.sources_by_type()

    assert scoped == {"gmail": 1, "slack": 1}
    assert everything.get("slack", 0) >= 1
