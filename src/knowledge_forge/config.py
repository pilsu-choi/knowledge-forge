from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """KF_-prefixed knobs. Anthropic credentials are resolved by the SDK
    itself (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or an `ant auth login`
    profile) - not duplicated here. Same principle for OpenRouter:
    OPENROUTER_API_KEY is read directly from the environment by the
    OpenRouter provider classes at construction time, not stored here.
    """

    model_config = SettingsConfigDict(env_prefix="KF_", env_file=".env", extra="ignore")

    extraction_model: str = "claude-opus-5"
    max_tokens_per_chunk: int = 4000
    # Caps in-flight Anthropic API calls (extraction + vision) so a large
    # batch doesn't fan out unbounded concurrent requests.
    max_concurrent_llm_requests: int = 5
    # How many consecutive chunks (see extraction/grouping.py) are bundled
    # into one Extractor.extract() call. 1 (default) reproduces the exact
    # prior one-chunk-per-call behavior; >1 lets a KnowledgeObject whose
    # evidence spans an adjacent chunk boundary (see roadmap.md 5단계 item
    # 1) actually be grounded in more than one chunk, at the cost of a
    # larger prompt per call.
    extraction_chunk_group_size: int = 1
    # Same-document, same-run near-duplicate merging (roadmap.md 5단계 item
    # 2, dedup.py) - two chunks/chunk bundles that both extract "the same"
    # object get merged into one. 1.0 (or any value > 1.0) effectively
    # disables merging, since text_similarity() never reaches it for
    # non-identical text.
    dedup_similarity_threshold: float = 0.9
    # Matches a re-extraction's new objects against the prior run's objects
    # for the same source (roadmap.md 5단계 item 4, extraction/versioning.py)
    # to auto-fill `supersedes`/`version`. Only takes effect when a `store`
    # is configured, since there is nothing to compare against otherwise.
    supersedes_similarity_threshold: float = 0.8
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

    # SaaS collector credentials (KF_-prefixed, see collectors/saas/factory.py
    # for how these turn into an authenticated SDK client + Collector). All
    # optional - only the fields for the source(s) actually in use need to
    # be set; the corresponding build_*_collector() raises a clear error if
    # a required one is missing.
    slack_bot_token: str | None = None
    # Comma-separated channel IDs to sync, e.g. "C0123,C0456".
    slack_channels: str | None = None

    notion_token: str | None = None
    notion_database_id: str | None = None

    # Service-account credentials for Drive: either the JSON itself or a
    # path to the JSON file.
    gdrive_credentials_json: str | None = None
    # Folder to sync; omit to sync every file the service account can see.
    gdrive_folder_id: str | None = None

    # Gmail uses a pre-authorized user OAuth token (no service-account
    # delegation) - both are paths to files produced by a one-off OAuth
    # setup, not inline JSON.
    gmail_credentials_path: str | None = None
    gmail_token_path: str | None = None
    gmail_query: str | None = None


settings = Settings()
