from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """KF_-prefixed knobs. Anthropic credentials are resolved by the SDK
    itself (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or an `ant auth login`
    profile) - not duplicated here. Same principle for OpenRouter:
    OPENROUTER_API_KEY is read directly from the environment by the
    OpenRouter provider classes at construction time, not stored here.
    """

    model_config = SettingsConfigDict(env_prefix="KF_", extra="ignore")

    extraction_model: str = "claude-opus-5"
    max_tokens_per_chunk: int = 4000
    # Caps in-flight Anthropic API calls (extraction + vision) so a large
    # batch doesn't fan out unbounded concurrent requests.
    max_concurrent_llm_requests: int = 5
    # PostgreSQL DSN for Source/Document/Chunk/KnowledgeObject persistence.
    # None disables storage - the pipeline runs print-only, as before.
    database_url: str | None = None
    # Root directory for the content-addressed raw blob store (RawBlobStore).
    data_dir: str = "./data"

    # Which LLM backend Knowledge Extraction and Vision extraction use.
    # "anthropic" (default) is the production-tested path; "openrouter" is
    # implemented and unit-tested against mocks only - see
    # wiki/llm-providers.md for the current verification status.
    llm_provider: Literal["anthropic", "openrouter"] = "anthropic"
    openrouter_extraction_model: str = "anthropic/claude-haiku-4.5"
    openrouter_vision_model: str = "anthropic/claude-haiku-4.5"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"


settings = Settings()
