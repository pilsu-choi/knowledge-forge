"""Suite-wide fixtures.

The only thing here is an isolation guard, and it exists because of a real
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
"""

import pytest

from knowledge_forge.config import Settings, settings
from knowledge_forge.embedding.cache import query_embedding_cache

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


@pytest.fixture(autouse=True)
def _clear_query_embedding_cache():
    """`vector_search`의 질의 임베딩 캐시(embedding/cache.py)를 테스트마다 비운다.

    캐시 키가 `(embedding_model, query)`뿐이라, 같은 모델 이름 + 같은 질의 문자열을 쓰는
    두 테스트가 서로 다른 가짜 임베더를 monkeypatch해도 두 번째 테스트는 첫 번째의 벡터를
    받는다. 프로덕션에서는 그게 정확히 원하는 동작이고(같은 모델이면 같은 벡터), 테스트에서만
    '앞 테스트가 어떤 벡터를 심었는지'에 결과가 달라지는 순서 의존이 된다 - 위의 설정 핀 고정과
    같은 이유(스위트가 자기 밖의 상태를 읽지 않게)로 여기서 끊는다."""
    query_embedding_cache.clear()
    yield
    query_embedding_cache.clear()
