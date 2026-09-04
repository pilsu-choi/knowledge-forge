"""Why KF_EMBEDDING_API_KEY exists at all (config.py's Settings docstring):
pydantic-settings reads `.env` into Settings but never exports it to
os.environ, and nothing in this repo calls load_dotenv(). These tests pin
both halves of that - the KF_-prefixed key IS readable from a .env file, and
a non-prefixed OPENAI_API_KEY line in the same file reaches nothing.
"""

import os

from knowledge_forge.config import Settings


def test_embedding_credentials_are_readable_from_a_dotenv_file(tmp_path, monkeypatch):
    monkeypatch.delenv("KF_EMBEDDING_API_KEY", raising=False)
    monkeypatch.delenv("KF_EMBEDDING_PROVIDER", raising=False)
    monkeypatch.delenv("KF_EMBEDDING_BASE_URL", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "KF_EMBEDDING_PROVIDER=openrouter\n"
        "KF_EMBEDDING_API_KEY=sk-or-from-dotenv\n"
        "KF_EMBEDDING_BASE_URL=https://openrouter.ai/api/v1\n"
        "KF_EMBEDDING_MODEL=openai/text-embedding-3-small\n"
    )

    settings = Settings(_env_file=str(env_file))

    assert settings.embedding_provider == "openrouter"
    assert settings.embedding_api_key == "sk-or-from-dotenv"
    assert settings.embedding_base_url == "https://openrouter.ai/api/v1"
    assert settings.embedding_model == "openai/text-embedding-3-small"


def test_a_non_kf_prefixed_key_in_dotenv_does_not_reach_the_process_environment(tmp_path, monkeypatch):
    """The gap KF_EMBEDDING_API_KEY works around: the provider classes' last
    fallback is os.environ.get("OPENAI_API_KEY"), which a .env line cannot
    fill. If this ever starts failing, load_dotenv() was added somewhere and
    the exception in config.py's docstring should be revisited."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=sk-should-not-leak\n")

    Settings(_env_file=str(env_file))

    assert os.environ.get("OPENAI_API_KEY") is None
