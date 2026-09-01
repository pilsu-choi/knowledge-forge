from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """KF_-prefixed knobs. Anthropic credentials are resolved by the SDK
    itself (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or an `ant auth login`
    profile) - not duplicated here.
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


settings = Settings()
