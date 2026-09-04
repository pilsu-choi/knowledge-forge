"""Suite-wide fixtures and hooks.

Two guards live here, both against the same class of problem: a test run that
looks green while it is quietly measuring less than it claims to.

The first is an `.env` isolation guard, and it exists because of a real
failure rather than as a precaution: `Settings` (config.py) is a module-level
singleton built with `env_file=".env"`, so the values it holds depend on
whatever the developer happens to have in the repo's `.env` at the moment
pytest imports `knowledge_forge.config`. Every test that asserts on a
settings-derived default was therefore only passing on machines where that
setting was unset.

Configuring embeddings for real (`KF_EMBEDDING_PROVIDER=openrouter`,
`KF_EMBEDDING_MODEL=openai/text-embedding-3-small`,
`KF_EMBEDDING_SOURCE=chunk` - see wiki/embeddings.md) turned four previously
green tests red without a line of source changing: the factory returned
OpenRouter where the test expected OpenAI, `vector_search` forwarded the
slug-prefixed model name, and pipeline.py embedded chunks instead of
KnowledgeObjects so the KO-grain assertion saw nothing embedded at all.
None of those were bugs - the suite was just reading the developer's config.

Only the `embedding_*` group is pinned, deliberately:

- It is the group that broke, and the group most likely to be set locally now
  that a `.env` can carry the credential (config.py's `embedding_api_key`).
- Pinning *every* field would break the tests that are supposed to read the
  environment - `KF_TEST_DATABASE_URL` above all, which is how DB-backed
  tests find Postgres in the first place.

Defaults are read back off the model fields rather than repeated as literals
here, so changing a default in config.py doesn't silently leave a stale copy
behind in the test harness.

The second guard is `KF_REQUIRE_DB_TESTS` at the bottom of this file: it turns
"the Postgres-backed tests all skipped" from a green run into a red one. See
the comment there for why the default skip-if-no-DSN convention needed one.
"""

import os

import pytest

from knowledge_forge.config import Settings, settings

# Every field the `.env` embedding block can set. Kept as an explicit list
# rather than a `startswith("embedding_")` scan so that adding a setting is a
# deliberate decision about whether the suite should see the local value.
_PINNED_EMBEDDING_FIELDS = (
    "embedding_enabled",
    "embedding_provider",
    "embedding_model",
    "embedding_api_key",
    "embedding_base_url",
    "embedding_dimensions",
    "embedding_source",
)


@pytest.fixture(autouse=True)
def _pin_embedding_settings_to_defaults(monkeypatch):
    """Makes the suite's view of the embedding config independent of `.env`.

    Mutates the shared `settings` object rather than swapping it out, because
    modules import it by value (`from knowledge_forge.config import settings`)
    - rebinding the name in config.py would leave every one of those importers
    still holding the old object. monkeypatch restores the previous values
    after each test, so a test that wants a different value can still set one.
    """
    for field in _PINNED_EMBEDDING_FIELDS:
        monkeypatch.setattr(settings, field, Settings.model_fields[field].default)


# --- DB 테스트가 조용히 건너뛰어지는 것을 막는 가드 --------------------------
#
# tests/storage/, tests/search/test_retrieve.py, tests/test_worker.py 등 89개
# 테스트는 `KF_TEST_DATABASE_URL`이 없으면 `pytest.mark.skipif`으로 통째로
# 건너뛴다. 개발자 로컬에서는 합리적인 규약이지만 실패 방식이 조용하다는 게
# 문제다 - DSN을 빠뜨린 CI는 `789 passed, 89 skipped`를 초록불로 보고하고,
# postgres_store.py(리포지토리 최대 모듈이자 pgvector/pg_trgm 검색 SQL 전부)는
# 아무도 눈치채지 못한 채 회귀 방어를 잃는다.
#
# `KF_REQUIRE_DB_TESTS`가 켜져 있으면 그 skip을 실패로 승격한다.
# CI(.github/workflows/ci.yml)는 항상 켜므로 Postgres 서비스나 DSN 설정이
# 깨지면 초록불 대신 빨간불이 난다. 기본값은 꺼짐이라 Postgres 없이
# `uv run pytest`를 돌리던 기존 로컬 흐름은 그대로다.
_DB_SKIP_REASON = "KF_TEST_DATABASE_URL"


def _require_db_tests() -> bool:
    return os.environ.get("KF_REQUIRE_DB_TESTS", "").strip().lower() in {"1", "true", "yes", "on"}


def pytest_configure(config):
    """DSN이 비어 있는 경우를 89개 실패가 아니라 한 줄로 끝낸다.

    가장 흔한 사고(워크플로에서 `env:` 한 줄 누락)를 수집 전에, 무엇이
    잘못됐는지 바로 읽히는 메시지로 잡기 위한 것이다. DSN은 있는데 DB
    테스트가 여전히 건너뛰어지는 나머지 경우는 아래 훅이 담당한다."""
    if _require_db_tests() and not os.environ.get("KF_TEST_DATABASE_URL"):
        raise pytest.UsageError(
            "KF_REQUIRE_DB_TESTS is set but KF_TEST_DATABASE_URL is empty - every "
            "DB-backed test would skip and the run would still be green. Point "
            "KF_TEST_DATABASE_URL at a Postgres that has the `vector` and `pg_trgm` "
            "extensions available, or unset KF_REQUIRE_DB_TESTS to run the suite "
            "without a database."
        )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    """DB 부재로 인한 skip을 실패로 바꾼다(`KF_REQUIRE_DB_TESTS`일 때만).

    skip 사유 문자열로 판단하므로, 앞으로 다른 조건(예: pgvector 확장 부재)의
    DB 관련 skipif가 추가돼도 사유에 환경변수 이름만 남아 있으면 함께 잡힌다."""
    report = yield
    if _require_db_tests() and report.skipped and _DB_SKIP_REASON in str(report.longrepr):
        report.outcome = "failed"
        report.longrepr = f"KF_REQUIRE_DB_TESTS is set, but this DB-backed test skipped: {report.longrepr}"
    return report
