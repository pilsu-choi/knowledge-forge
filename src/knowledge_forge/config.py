from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """KF_-prefixed knobs. Anthropic credentials are resolved by the SDK
    itself (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or an `ant auth login`
    profile) - not duplicated here. Same principle for OpenRouter:
    OPENROUTER_API_KEY is read directly from the environment by the
    OpenRouter provider classes at construction time, not stored here. Same
    again for OPENAI_API_KEY (embedding/openai_provider.py). The agent
    runtime (agent/providers/) reuses ANTHROPIC_API_KEY/OPENROUTER_API_KEY
    rather than minting its own env var names, for the same reason.
    """

    model_config = SettingsConfigDict(env_prefix="KF_", env_file=".env", extra="ignore")

    extraction_model: str = "claude-opus-5"
    max_tokens_per_chunk: int = 4000
    # Caps in-flight Anthropic/OpenAI API calls (extraction + vision +
    # embedding) so a large batch doesn't fan out unbounded concurrent
    # requests. Embedding reuses this rather than getting its own knob -
    # it's the same "don't fan out unbounded requests" concern, and a
    # dedicated setting isn't justified until it's actually a bottleneck.
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

    # Root directory an OKF file tree lives under (same meaning as
    # extract/schedule's --okf-dir, api/routes/okf.py's `GET /okf/*` reads
    # it directly off disk - no Postgres involved). None disables those
    # routes (api/deps.py::get_okf_root), same convention as database_url.
    okf_dir: str | None = None

    # Celery broker + result backend (celery_app.py) - unlike database_url,
    # this has a real default rather than None: the Celery app object needs
    # a concrete broker URL to construct, and `POST /ingest` can't dispatch
    # jobs without one. Assumes a locally-running Redis for bare `serve`/
    # `worker` runs; docker-compose overrides this to the `redis` service.
    redis_url: str = "redis://localhost:6379/0"

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
    claude_agent_sdk_extraction_model: str = "claude-haiku-4-5-20251001"
    # Only needed when `claude` isn't on PATH under that exact name.
    claude_agent_sdk_cli_path: str | None = None

    # Whether `extract`/`schedule` embed each document's merged
    # KnowledgeObjects into knowledge_object_embeddings right after storing
    # them (opt-in, mirrors database_url's "off unless configured"
    # convention) - see wiki/embeddings.md. Only takes effect when a store
    # is configured (embedding needs somewhere to write to). `--embed`/
    # `--no-embed` on `extract` overrides this per invocation.
    embedding_enabled: bool = False
    # Switch point for a future non-OpenAI embedding backend (mirrors
    # llm_provider/get_extractor) - only "openai" is implemented so far.
    embedding_provider: Literal["openai"] = "openai"
    embedding_model: str = "text-embedding-3-small"
    # Must match the `vector(N)` dimension in schema.sql's
    # knowledge_object_embeddings table - changing embedding_model to one
    # with a different output dimension requires updating both together.
    embedding_dimensions: int = 1536
    # Which text gets embedded for a KnowledgeObject (wiki/embeddings.md's
    # "청크 기반 vs KnowledgeObject 기반" comparison) - "knowledge_object"
    # (default) embeds the LLM's summary/content/topics (higher retrieval
    # precision, dedup'd, but only as good as the extraction and unavailable
    # until extraction finishes); "chunk" embeds the raw evidence chunk text
    # instead (no extraction-quality ceiling, embeddable as soon as chunking
    # finishes, but noisier and not deduped). Same knowledge_object_embeddings
    # table/schema either way - this only changes which text is sent to the
    # embedding provider, not the storage grain. Switch per run with `--embed`
    # or `knowledge-forge embed`'s `--embedding-source` if the choice needs
    # to differ from this default (e.g. speed problems -> "chunk", precision
    # problems -> "knowledge_object").
    embedding_source: Literal["knowledge_object", "chunk"] = "knowledge_object"

    # Agent runtime (harness, wiki/agent-runtime.md) - a separate switch from
    # `llm_provider` above, deliberately: `llm_provider` picks Knowledge
    # Extraction's backend, `agent_provider` picks the multi-turn tool-calling
    # harness's backend, and the two concerns can legitimately point at
    # different backends (e.g. extraction on Anthropic, agent chat routed
    # through OpenRouter to a different model). "claude_agent_sdk" is
    # deliberately NOT a valid value here - see agent-runtime.md's
    # "claude_agent_sdk는 harness의 어떤 모드에도 들어오지 않는다": that SDK
    # owns its own tool-call loop internally, so it can't be plugged in as a
    # stateless "send one turn" adapter alongside the two below without the
    # harness's own loop (agent/loop.py) duplicating it.
    agent_provider: Literal["anthropic", "openai_compatible"] = "anthropic"
    agent_model: str = "claude-sonnet-5"
    agent_openai_compatible_model: str = "anthropic/claude-sonnet-5"
    # Reuses the same default as openrouter_base_url (both are "point an
    # OpenAI-compatible client somewhere") - kept as a separate key so a
    # deployment can run extraction against OpenRouter and the agent against
    # a different OpenAI-compatible endpoint (e.g. a local vLLM server)
    # without the two colliding.
    agent_openai_compatible_base_url: str = "https://openrouter.ai/api/v1"
    # Hard cap on tool-call round trips in agent/loop.py::run_agent() - not a
    # tuning knob so much as a safety net against a model that keeps calling
    # tools without ever producing a final answer.
    agent_max_iterations: int = 8
    # Runtime context compaction (agent/compaction.py, ported from
    # refs/hermes-agent's trajectory_compressor.py) - once `messages` in
    # agent/loop.py::run_agent crosses this token budget, the oldest
    # compressible middle turns get replaced by one summary message
    # generated via the SAME ChatProvider already driving the session (no
    # separate summarization model/API key, unlike hermes's dedicated
    # OpenRouter client). `protect_tail` is how many of the most recent
    # messages are kept verbatim no matter what, so the model never loses
    # the round trip it just did.
    agent_context_compaction_enabled: bool = True
    agent_context_compaction_max_tokens: int = 100_000
    agent_context_compaction_protect_tail: int = 8
    # Cross-session agent memory (agent/memory.py) - plain markdown files,
    # one per project_id (plus a "_global" file), stored OUTSIDE the OKF
    # tree on purpose: agent-authored notes must never get mixed into the
    # knowledge base itself or picked up by file_search/OKFWriter.
    # Reuses the ".knowledge-forge-state/" convention `collectors/local_file.py`
    # already put in .gitignore - one root for this-machine-only runtime state.
    agent_memory_dir: str = "./.knowledge-forge-state/agent-memory"

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
