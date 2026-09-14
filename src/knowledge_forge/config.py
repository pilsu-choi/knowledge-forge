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

    `embedding_api_key` below is the one deliberate exception to that rule,
    and the reason is mechanical rather than stylistic: pydantic-settings
    loads `.env` into THIS class only - it does not export those values into
    `os.environ`, and nothing in this repo calls `load_dotenv()`. So a bare
    `OPENAI_API_KEY=`/`OPENROUTER_API_KEY=` line in `.env` is read by nobody:
    the provider classes look it up with `os.environ.get()`, which only sees
    variables actually exported into the process. Every other credential
    above belongs to an SDK/CLI that people already export (or that Docker
    Compose passes through), but "put the embedding key in .env" is a
    reasonable thing to expect and used to silently do nothing - hence one
    KF_-prefixed key that .env CAN carry. The env-var paths still win nothing
    and lose nothing: they stay as the last fallback in both providers.
    """

    model_config = SettingsConfigDict(env_prefix="KF_", env_file=".env", extra="ignore")

    extraction_model: str = "claude-opus-5"
    max_tokens_per_chunk: int = 4000
    # AdaptiveChunker's min_tokens (chunking/adaptive.py): a leaf chunk under
    # this many tokens is a candidate for the bottom-up merge with an
    # adjacent sibling leaf (a lone bullet, a title-only slide paragraph)
    # rather than being emitted on its own. Never merges across a
    # Chapter/Section boundary or mixes a table chunk with a non-table one -
    # see AdaptiveChunker._merge_small_siblings().
    min_tokens_per_chunk: int = 50
    # Caps in-flight Anthropic/OpenAI API calls (extraction + vision +
    # embedding) so a large batch doesn't fan out unbounded concurrent
    # requests. Embedding reuses this rather than getting its own knob -
    # it's the same "don't fan out unbounded requests" concern, and a
    # dedicated setting isn't justified until it's actually a bottleneck.
    max_concurrent_llm_requests: int = 5
    # How many documents run_pipeline (pipeline.py) processes at once -
    # parse, chunk, store, embed and extract. LLM/VLM/embedding calls stay
    # capped by max_concurrent_llm_requests above; this only stops one slow
    # document from holding up the rest. 1 restores strictly sequential
    # processing. See wiki/architecture.md "문서 간 동시성".
    max_concurrent_documents: int = 4
    # Token budget for bundling consecutive chunks (see
    # extraction/grouping.py) into one Extractor.extract() call. A single
    # chunk already at or over this budget (e.g. a whole Chapter under
    # AdaptiveChunker's own budget) stays alone in its own call; several
    # small chunks get bundled up to this many total tokens, which is what
    # actually cuts LLM call count for a document with many small chunks -
    # a call-count knob (the prior KF_EXTRACTION_CHUNK_GROUP_SIZE) can't
    # express "chunk sizes vary" the way a token budget does. Bundling also
    # lets a KnowledgeObject whose evidence spans an adjacent chunk boundary
    # (see roadmap.md 5단계 item 1) actually be grounded in more than one
    # chunk.
    extraction_group_max_tokens: int = 4000
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
    # Separate knob from claude_agent_sdk_extraction_model, mirroring
    # openrouter_extraction_model/openrouter_vision_model - same default
    # today, but table/figure/image transcription and Knowledge Extraction
    # are different workloads that may want different models later. See
    # vision/claude_agent_sdk_extractor.py.
    claude_agent_sdk_vision_model: str = "claude-haiku-4-5-20251001"
    # Only needed when `claude` isn't on PATH under that exact name.
    claude_agent_sdk_cli_path: str | None = None

    # Whether `extract`/`schedule` embed each document's merged
    # KnowledgeObjects into knowledge_object_embeddings right after storing
    # them (opt-in, mirrors database_url's "off unless configured"
    # convention) - see wiki/embeddings.md. Only takes effect when a store
    # is configured (embedding needs somewhere to write to). `--embed`/
    # `--no-embed` on `extract` overrides this per invocation.
    # `search/retrieve.py`의 term 단위 키워드 fallback 튜닝 노브 - 임베딩이 없거나
    # vector가 빈손일 때 질의를 term으로 쪼개 각각 조회하고, "질의의 term을 몇 개나 담았나"로
    # 후보를 거른다. ratio는 term 수 대비 비율, min은 그 하한(term이 두세 개뿐인 짧은 질의에서
    # 비율만 쓰면 1개만 겹쳐도 통과한다). 0으로 두면 필터가 꺼진다 - 재현율은 오르지만 "관련
    # 문서 없음"이 정답인 질의(null/negative)와 개수 질의가 무너진다.
    # 기본값 2/3은 v0 골든셋 스윕에서 고른 값이다: 이보다
    # 느슨하면(0.5~0.6) ndcg는 조금 더 오르지만 none_correct가 1.000에서 내려가고, 더 조이면
    # (0.75) 재현율이 baseline 쪽으로 되돌아간다. 2/3은 baseline 대비 어느 지표도 나빠지지 않는
    # 유일한 지점이었다.
    # 주의: 임계값은 `ceil(term수 × ratio)`라 정확히 나누어떨어지는 지점에서 계단이 진다 -
    # 2/3과 0.67은 term이 3·6·9개일 때 임계값이 다르다(2/3 → 2·4·6, 0.67 → 3·5·7). 반올림
    # 문제처럼 보이지만 실제로 랭킹이 바뀐다(v0 실측 ndcg@10 0.845 vs 0.839). 이 값을 손댈 때는
    # 소수점 두 자리로 어림하지 말고 실제로 재라.
    term_coverage_ratio: float = 2 / 3
    term_coverage_min: int = 2
    # 위의 개수 기준과 **함께**(AND) 거는 변별력 기준 - `search/retrieve.py::_required_terms`.
    # 개수만 세면 코퍼스가 커질수록 필연적으로 무너진다: 흔한 term끼리 우연히 겹치는 문서는
    # 코퍼스 크기에 비례해 늘어나기 때문이다. 실제로 코드 변경 없이 263 → 326 문서(24% 증가)
    # 만으로 v0 none_correct가 1.000 → 0.667로 내려갔다("Kubernetes 클러스터 구성 가이드"가
    # 흔한 term 3개만 겹친 문서를 물어 왔다). 그래서 "질의에서 df가 가장 낮은 term(들)을 그
    # 문서가 반드시 담아야 한다"를 추가로 요구한다. 이 값은 그 최소 df 위에 얹는 **절대 하한**
    # 이다: 0이면 순수하게 "가장 희귀한 term(동점이면 전부)"만 요구하고, 올릴수록 df가 그 이하인
    # term을 전부 요구해 더 조인다. 음수면 이 게이트를 끈다(옛 개수 전용 동작).
    #
    # 기본 0은 v0 골든셋 스윕(-1/0/1/2/3/4/5/6/8)에서 고른 값이다.
    # 0 이상이면 어디서든 none_correct 1.000 / recall@5 0.888 / exhaustive_recall 0.908로
    # 필수 조건을 만족하고, ndcg@10만 0~2에서 0.886, 3~5에서 0.892(질의 2개 차이, 95% CI가 0을
    # 포함)로 갈린 뒤 8에서 절벽이다(recall@5 0.881 / exhaustive_recall 0.891).
    # 더 높은 고원(3~5)의 +0.006을 일부러 버렸다. 이유는 이 노브가 **절대** df라 코퍼스가 작을수록
    # 더 조이기 때문이다 - 지금 코퍼스(326문서)에서 cap=4는 질의 term의 평균 30%를 요구하지만
    # 코퍼스가 1/3로 작아지면 같은 4가 거의 모든 term을 요구해, 재현율이 무너진 cap=8(평균 45%
    # 요구)과 같은 자리에 선다. 이번 수정 자체가 "코퍼스 크기에 따라 무너지는 판정을 없앤다"는
    # 것이므로, 질의 두 개짜리 이득보다 크기 불변성을 택했다. 0은 최소 df만 따라가므로 코퍼스가
    # 커지든 작아지든 "질의에서 가장 희귀한 term"이라는 의미가 유지된다.
    # (구현상 0과 1은 완전히 같은 규칙이다: 최소 df가 1 이상이면 하한 1이 아무 영향이 없고,
    # 최소 df가 0이면 그 term을 담은 문서가 없어 어차피 전부 기각이다.)
    term_rare_df_max: int = 0
    # `latest` 후처리가 시각 내림차순으로 재정렬할 때 대상으로 삼는 **관련도 상위 문서 수**.
    # 전체 후보를 시각으로 정렬하면 관련도 랭킹이 통째로 버려져 "질의와 상관없이 가장 최근
    # 문서"가 1위가 된다 - 후보가 늘어날수록 나빠진다(v0-latest-001/002가 그렇게 0.000이 됐다).
    # 기본 5는 v0 스윕에서 고른 값이다: 7 이상이면 latest 0.544 /
    # version 0.947로 되돌아가고, 5 이하는 latest 0.667 / version 1.000으로 평평하다. 다만
    # 근거가 latest 3개 + version 7개뿐이라 잠정값이다 - 골든셋 v1에서 다시 재야 한다.
    latest_relevance_pool: int = 5

    # ANN(HNSW)에는 "관련도 하한"이 없다 - 코퍼스에 전혀 없는 주제를 물어도 가장 가까운
    # `limit`개를 무조건 돌려주므로, "관련 문서 없음"이 정답인 질의(golden의 negative/null)
    # 조차 후보가 찬다. exhaustive/count/negative는 컷오프 없이 그 후보 집합을 그대로 답으로
    # 쓰기 때문에 임베딩을 채우자 v0에서 none_correct 1.000 → 0.000, count_match 0.333 →
    # 0.000이 됐다(run 9ad2bba2). 이 값보다 낮은 코사인 유사도(`1 - distance`)의 벡터 히트를
    # 버린다. 0.0은 끄는 값(하한 없음)이고, 모델/코퍼스마다 달라지는 값이라 기본값은 추측이
    # 아니라 골든셋 스윕으로 정했다.
    #
    # 0.45는 v0 골든셋 76개 스윕(0.0/0.35/0.40/0.42~0.48/0.50)에서 고른 값이다. 0.42~0.47이
    # ndcg@10 0.876~0.888의 고원이고 none_correct 1.000 / count_match 0.333이 그 구간 내내
    # 유지된다. 최고점은 0.42(0.888)지만 기본값은 고원 한가운데로 잡았다 - v0는 사람 검수를
    # 거치지 않은 스모크셋이라 argmax를 박으면 과적합이고,
    # 0.40에서 none_correct가 0.833으로 무너지는 절벽과의 안전거리가 ndcg +0.002보다 낫다.
    # 0.0으로 두면 하한이 꺼지고 임베딩 채운 직후의 동작(ndcg 0.870)으로 돌아간다.
    vector_min_score: float = 0.45

    embedding_enabled: bool = False
    # Which embedding backend embedding/factory.py builds (mirrors
    # llm_provider/get_extractor). "openrouter" talks to OpenRouter's
    # OpenAI-compatible /embeddings endpoint - same wire protocol, so it is
    # the same `openai` SDK with a different base_url and key, exactly like
    # the extraction path's openrouter option.
    embedding_provider: Literal["openai", "openrouter"] = "openai"
    # The model name is provider-scoped: a bare OpenAI name for "openai"
    # ("text-embedding-3-small"), an OpenRouter `vendor/model` slug for
    # "openrouter" ("openai/text-embedding-3-small",
    # "qwen/qwen3-embedding-8b", ...). Deliberately one key rather than a
    # per-provider pair - see the comment in embedding/factory.py for why
    # embeddings differ from extraction here.
    embedding_model: str = "text-embedding-3-small"
    # The embedding API key, and the ONLY credential in this file - see the
    # class docstring for why this one exception exists (short version: a
    # non-KF_ key in `.env` is read by nothing). Applies to whichever
    # provider is active; each falls back to its own conventional env var
    # (OPENAI_API_KEY / OPENROUTER_API_KEY) when this is unset.
    embedding_api_key: str | None = None
    # Overrides the endpoint both providers point at. None means the
    # provider's own default (api.openai.com for "openai",
    # `openrouter_base_url` for "openrouter"). Set it to run "openai"
    # against any OpenAI-compatible server - a local vLLM/TEI/LM Studio
    # instance, an Azure-style gateway - without needing a third provider
    # implementation, which is the same reason
    # agent_openai_compatible_base_url exists for the agent runtime.
    embedding_base_url: str | None = None
    # Must match the `vector(N)` dimension in schema.sql's
    # knowledge_object_embeddings table - changing embedding_model to one
    # with a different output dimension requires updating both together.
    # embedding/validation.py checks the model's actual output against this
    # on every embed() call, because getting it wrong otherwise surfaces as
    # an opaque pgvector INSERT error much later; that matters more now that
    # "openrouter" puts 3072/4096-dim models one config line away.
    embedding_dimensions: int = 1536
    # Which grain gets embedded (wiki/embeddings.md's "청크 기반 vs
    # KnowledgeObject 기반" comparison) - "knowledge_object" (default) embeds
    # each KO's summary/content/topics after extraction (higher retrieval
    # precision, dedup'd, but only as good as the extraction and unavailable
    # until extraction finishes); "chunk" embeds the raw chunk text right
    # after chunking (no extraction-quality ceiling, searchable before - or
    # without - extraction, e.g. `extract --no-extract`, but noisier and not
    # deduped). Both land in the same knowledge_object_embeddings table,
    # distinguished by `target_kind` and joined through the shared
    # `document_id` (schema.sql). Switch per run with `extract`'s /
    # `embed`'s `--embedding-source` if the choice needs to differ from this
    # default (e.g. speed problems -> "chunk", precision problems ->
    # "knowledge_object").
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
    # 에이전트에게 노출할 **검색 도구 표면**(agent/tools.py::default_tools).
    #
    # "minimal"(기본): 검색 도구는 `search`(통합 검색, search/retrieve.py) +
    # `read_document` + `sql_search`뿐이다. `search/retrieve.py`가 존재하는 이유가
    # "어떤 검색을 어떤 순서로 돌릴지"를 LLM에게서 빼앗아 결정론적 함수로 만드는
    # 것인데(그 모듈 docstring), 도구 목록이 개별 검색 도구를 여덟 개 늘어놓으면
    # 그 결정을 다시 모델에게 돌려주는 셈이다. `sql_search`는 집계/속성 조건처럼
    # `search`가 다루지 못하는 구조화 질의용 비상구로 남기고, `read_document`는
    # 검색이 아니라 "특정한 문서를 끝까지 읽는" 후속 동작이라 남는다.
    # Postgres가 없는 OKF 전용 배포에서는 이 셋이 전부 빠지므로 `file_search`가
    # 대신 들어간다 - 그 구성에서 유일하게 동작하는 검색 경로다.
    #
    # "full": 예전 표면 그대로 - `file_search`/`vector_search`/`document_search`/
    # `lexical_search`/`plan_search`까지 전부 노출한다. `search`가 놓치는 질의를
    # 손으로 파고들 때(디버깅, A/B 비교)를 위해 지운 게 아니라 설정 뒤로 옮겼다.
    agent_tool_surface: Literal["minimal", "full"] = "minimal"
    # `file_search`(OKF 파일 트리 위의 부분 문자열 검색, `search/service.py`)를 에이전트
    # 도구 목록에 넣을지. **기본 False = 넣지 않는다.**
    #
    # 표면 설정과 무관하게 따로 스위치를 둔 이유: `file_search`는 다른 검색 도구와 코퍼스
    # 자체가 다르다(Postgres가 아니라 `KF_OKF_DIR` 아래의 마크다운 트리). 그래서
    #   - `KF_OKF_DIR`이 안 잡힌 배포에서는 에러가 아니라 조용히 `[]`를 돌려준다. 모델은
    #     "그런 문서가 없다"로 읽고 답을 접는다 - 도구가 없는 것보다 나쁘다.
    #   - 트리가 있어도 Postgres 코퍼스와 내용이 어긋나 있으면(인제스트와 `--okf-dir`
    #     출력은 서로 독립이다) 두 도구가 서로 다른 사실을 돌려준다.
    # OKF 전용(Postgres 없는) 배포에서는 이게 유일한 검색 경로이므로 그때는 켜야 한다.
    agent_file_search_enabled: bool = False
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
