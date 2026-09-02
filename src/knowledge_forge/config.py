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
    # "anthropic" (default) is the production-tested path; "openrouter" and
    # "claude_agent_sdk" are implemented and unit-tested against mocks only
    # - see wiki/llm-providers.md for the current verification status.
    llm_provider: Literal["anthropic", "openrouter", "claude_agent_sdk"] = "anthropic"
    openrouter_extraction_model: str = "anthropic/claude-haiku-4.5"
    openrouter_vision_model: str = "anthropic/claude-haiku-4.5"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # "claude_agent_sdk" drives the `claude` CLI as a subprocess (via the
    # claude_agent_sdk package) instead of calling the Anthropic Messages
    # API directly - the CLI resolves its own credentials (browser OAuth
    # login, a `claude setup-token` CLAUDE_CODE_OAUTH_TOKEN, or a plain
    # ANTHROPIC_API_KEY), so a machine that already has Claude Code logged
    # in needs no separate API key. See extraction/claude_agent_sdk_extractor.py.
    claude_agent_sdk_extraction_model: str = "claude-opus-5"
    # Only needed when `claude` isn't on PATH under that exact name.
    claude_agent_sdk_cli_path: str | None = None

    # SaaS collector credentials (KF_-prefixed, see collectors/saas/factory.py
    # for how these turn into an authenticated SDK client + Collector). All
    # optional - only the fields for the source(s) actually in use need to
    # be set; the corresponding build_*_collector() raises a clear error if
    # a required one is missing.
    slack_bot_token: str | None = None
    # Comma-separated channel IDs to sync, e.g. "C0123,C0456". Optional when
    # slack_channel_project_map is set - the map's keys are used as the
    # channel list in that case, so the two don't need to be kept in sync.
    slack_channels: str | None = None
    # Per-channel tenancy: inline JSON object or a path to one, e.g.
    # '{"C0123": "PRJ-A", "C0456": "PRJ-B"}'. Lets one Slack collector run
    # span several projects - each channel's items are tagged with its own
    # project_id (RawItem.project_id) instead of the run's single
    # --project-id. A channel not present in the map falls back to that
    # run-level --project-id.
    slack_channel_project_map: str | None = None

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

    # Sources to auto-sync on an interval, started from inside `serve`'s own
    # process (scheduling/auto_enqueue.py) so the API server alone is enough
    # to also drive periodic sync - no separate `knowledge-forge schedule`
    # process needed. Inline JSON array or a path to a file containing one
    # (same "inline-or-path" convention as slack_channel_project_map above),
    # each entry shaped like IngestRequest plus `interval_seconds`, e.g.
    # '[{"source": "local", "path": "/app/input", "interval_seconds": 600}]'.
    # None (default) disables auto-scheduling entirely.
    scheduled_sources: str | None = None


settings = Settings()
