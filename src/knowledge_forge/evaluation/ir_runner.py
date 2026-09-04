"""Golden-set IR evaluation runner (wiki/search-evaluation.md (C)) - the
glue between `golden.py` (what is relevant), `search/retrieve.py` (what came
back) and `ir_metrics.py` (how good that was), producing one `EvalRun`.

Design points:

- The retriever is **injected** (`retrieve_fn`) rather than imported at
  module level. `search/retrieve.py` is developed in parallel and its
  contract (`RetrieveFn`/`RetrievalResultLike` below) is all this module
  relies on; the default resolves `knowledge_forge.search.retrieve.retrieve`
  lazily so the runner imports (and is unit-testable with a fake) even
  when that module is absent.
- One failing query must not sink the run: retrieval exceptions land in
  that query's `error` column and it contributes no metrics. A 76-query
  run that dies on query 40 because one strategy hit a timeout would
  otherwise leave no usable record at all.
- `limit` is **promoted to 200** for exhaustive/count queries. Those are
  scored on the whole result list (`exhaustive_recall`, `count_match`),
  and a 10-result cap would make a "list every X" query fail by
  construction, not by retrieval quality.
- Stale gold entries (`check_staleness`) are *reported*, not dropped - the
  scores are still computed so a stale run is comparable with the previous
  one, but the summary lists them so a drop can be attributed to drift
  rather than to a retrieval change.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import subprocess
import time
import traceback
import uuid
from collections.abc import Awaitable, Iterable, Mapping, Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol

from knowledge_forge.evaluation.golden import SPLITS, GoldenQuery, GoldenSet, check_staleness
from knowledge_forge.evaluation.ir_metrics import DEFAULT_K_VALUES, QueryScore, aggregate, score_query
from knowledge_forge.storage.eval_store import EvalRun, EvalRunQuery, EvalStore

if TYPE_CHECKING:
    from knowledge_forge.chat_provider import ChatProvider
    from knowledge_forge.storage import PipelineStore

# Result-list size for queries scored on the whole list (exhaustive / count).
EXHAUSTIVE_LIMIT = 200


class RetrievalResultLike(Protocol):
    """The slice of `search.retrieve.RetrievalResult` the runner reads."""

    document_ids: list[str]
    scores: list[float]
    predicted_count: int | None
    strategies: list[str]
    elapsed_ms: float

    # read-only: `RetrievalResult.steps` is `list[RetrievalStep]`, and a mutable
    # protocol attribute would demand an invariant match with `Sequence[Any]`.
    @property
    def steps(self) -> Sequence[Any]: ...


class RetrieveFn(Protocol):
    def __call__(
        self,
        query: str,
        *,
        store: PipelineStore,
        provider: ChatProvider | None,
        project_id: str | None,
        now: datetime | None,
        limit: int,
        use_llm_constraints: bool,
        strategies_override: list[str] | None,
    ) -> Awaitable[RetrievalResultLike]: ...


def default_retrieve_fn() -> RetrieveFn:
    from knowledge_forge.search.retrieve import retrieve

    return retrieve


def config_hash(config: Mapping[str, Any]) -> str:
    """First 12 hex chars of sha256 over the canonical (sorted-key) JSON -
    enough to group runs by settings, short enough to print in a listing."""
    canonical = json.dumps(config, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


# --- 실행된 코드의 신원 (git_commit) ------------------------------------
#
# `git rev-parse HEAD`만 기록하면 "마지막 커밋"은 남지만 "실제로 돌아간 코드"는
# 남지 않는다. 실제로 이 프로젝트의 기록이 그렇게 망가졌다: 커밋 b470057 하나에
# ablate-vector(ndcg@10 0.736)와 purity-vector-only(0.477)가 같은 git_commit으로
# 나란히 적혔고(그 중 후자는 나중에 97c0c50으로 커밋된 수정된 작업 트리였다),
# eval_run 표만 봐서는 어느 숫자가 어느 코드의 것인지 알 방법이 없다. worktree로
# 여러 에이전트가 병렬로 일하면 "측정 먼저, 커밋 나중"이 오히려 정상 경로라
# 이 충돌은 예외가 아니라 기본값이다.
#
# 그래서 기록 값을 "커밋"이 아니라 **돌아간 코드의 식별자**로 바꾼다:
#
#   깨끗한 트리: "<40hex sha>"                     (예전과 완전히 동일)
#   더러운 트리: "<40hex sha>+dirty.<12hex>"       (12hex = 작업 트리 diff 해시)
#   git이 아님 : None                              (tarball 체크아웃도 죽지 않아야 함)
#
# 커밋 해시를 **접두사로** 유지하는 게 핵심이다. `git_commit LIKE 'b470057%'`,
# `substring(git_commit, 1, 7)` 같은 기존 조회/그룹핑이 그대로 살아 있고, 새 컬럼을
# 모르는 읽는 쪽(생 SQL, 예전 대시보드)도 두 dirty run을 절대 같은 값으로 보지
# 않는다. dirty 여부만 기록하는 안(bool 한 칸)은 위 사고를 못 막는다 - 서로 다른
# 두 dirty 상태가 여전히 같은 값으로 뭉개지기 때문에, 해시까지 넣어야 "다른
# 파이프라인인데 기록이 같다"가 불가능해진다.
DIRTY_MARKER = "+dirty."

# untracked 파일 내용을 해시할 때 파일당 읽는 상한. .gitignore에 안 걸린 대용량
# 산출물이 섞여 있어도 run 시작이 느려지지 않게 자르되, 잘린 파일은 크기를 같이
# 섞어 넣어 "앞부분만 같은 서로 다른 파일"이 뭉개질 확률을 줄인다.
UNTRACKED_READ_LIMIT = 1 << 20


def _git(*args: str) -> str | None:
    """git 호출 한 번. 실패(비-git 디렉터리, git 미설치, 타임아웃, 0 아닌 종료)는
    전부 `None`으로 눌러서 프로버넌스 수집이 eval 실행 자체를 깨지 못하게 한다."""
    try:
        out = subprocess.run(["git", *args], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def current_git_commit() -> str | None:
    """HEAD 커밋만. 작업 트리 상태는 보지 않는다 - 이걸 그대로 기록하는 게 위
    주석의 사고였으므로, 기록에는 `current_code_provenance().identity`를 쓴다."""
    out = _git("rev-parse", "HEAD")
    return out.strip() if out and out.strip() else None


def _hash_untracked(digest: hashlib._Hash) -> None:
    """추적되지 않은(=아직 git이 모르는) 파일들의 내용을 해시에 섞는다.
    `git diff HEAD`는 tracked 파일만 보기 때문에, 새로 만든 모듈 하나가 파이프라인을
    바꿔 놓아도 diff 해시가 안 움직이는 구멍이 생긴다. `--exclude-standard`라 .gitignore
    대상은 애초에 목록에 없다(= out/, .venv 같은 건 안 읽는다)."""
    listing = _git("ls-files", "--others", "--exclude-standard", "--full-name", "-z")
    root = (_git("rev-parse", "--show-toplevel") or "").strip()
    if not listing or not root:
        return
    for rel in sorted(p for p in listing.split("\0") if p):
        digest.update(b"\0U\0" + rel.encode("utf-8", "surrogateescape"))
        path = os.path.join(root, rel)
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as fh:
                blob = fh.read(UNTRACKED_READ_LIMIT)
        except OSError:
            # 읽을 수 없는 건 "읽을 수 없었다"로 기록. 조용히 건너뛰면 그 파일이
            # 없는 트리와 같은 해시가 되어 버린다.
            digest.update(b"\0unreadable")
            continue
        digest.update(f"\0{size}\0".encode())
        digest.update(hashlib.sha256(blob).digest())


@dataclasses.dataclass(frozen=True)
class CodeProvenance:
    """실행 시점의 코드 상태. `identity`가 `eval_run.git_commit`에 들어간다."""

    commit: str | None
    # `git status --porcelain` 줄 그대로(예: " M src/.../retrieve.py", "?? foo.py").
    # 해시는 "달랐다"만 말해 주므로, 몇 달 뒤에 그 run을 읽는 사람이 *무엇이*
    # 달랐는지 보려면 파일 목록이 필요하다. .gitignore를 존중하니 크기는 커밋
    # 가능한 변경분 수준으로 제한된다.
    dirty_files: list[str]
    # sha256(status + `git diff HEAD` + untracked 내용)[:12]. 깨끗하면 None.
    diff_hash: str | None

    @property
    def identity(self) -> str | None:
        if self.diff_hash is None:
            return self.commit
        # 커밋이 없는 저장소(unborn branch)에서도 두 dirty 상태는 구분돼야 한다.
        return f"{self.commit or 'nocommit'}{DIRTY_MARKER}{self.diff_hash}"


def current_code_provenance() -> CodeProvenance:
    commit = current_git_commit()
    status = _git("status", "--porcelain")
    if status is None:
        # git 저장소가 아니다 - 예전과 동일하게 커밋 None, dirty 판정 없음.
        return CodeProvenance(commit=commit, dirty_files=[], diff_hash=None)
    entries = [line for line in status.splitlines() if line.strip()]
    if not entries:
        return CodeProvenance(commit=commit, dirty_files=[], diff_hash=None)
    digest = hashlib.sha256()
    digest.update(status.encode("utf-8", "surrogateescape"))  # 파일 목록 + 상태 코드
    digest.update(b"\0D\0")
    digest.update((_git("diff", "HEAD") or "").encode("utf-8", "surrogateescape"))  # tracked 내용(staged+unstaged)
    _hash_untracked(digest)
    return CodeProvenance(commit=commit, dirty_files=entries, diff_hash=digest.hexdigest()[:12])


def base_commit(git_commit: str | None) -> str | None:
    """기록된 코드 식별자에서 커밋 부분만 떼어 낸다(dirty 표식 제거)."""
    if not git_commit:
        return None
    return git_commit.split(DIRTY_MARKER, 1)[0] or None


def is_dirty_commit(git_commit: str | None) -> bool:
    """이 run이 수정된 작업 트리에서 측정됐는가. False는 "깨끗했다"가 아니라
    "깨끗했거나, 이 표기가 생기기 전의 옛 행이라 알 수 없다"에 가깝다 - 위 사고의
    26개 행(전부 맨 b470057)이 정확히 그 후자다."""
    return git_commit is not None and DIRTY_MARKER in git_commit


def format_code_identity(git_commit: str | None, *, short: int = 7) -> str:
    """사람이 읽는 짧은 표기: `b470057`, `b470057+dirty`, 없으면 `-`."""
    commit = base_commit(git_commit)
    if commit is None:
        return "-"
    return f"{commit[:short]}+dirty" if is_dirty_commit(git_commit) else commit[:short]


def effective_limit(q: GoldenQuery, limit: int) -> int:
    if q.expected.exhaustive or q.expected.count is not None:
        return max(limit, EXHAUSTIVE_LIMIT)
    return limit


def select_queries(
    golden: GoldenSet,
    query_filter: set[str] | None = None,
    type_filter: set[str] | None = None,
    split_filter: str | None = None,
) -> list[GoldenQuery]:
    """`type_filter` matches any of a query's (multi-label) `types`, not
    only `primary_type` - asking for `entity_centric` should include the
    relative_temporal query that is *also* entity-centric.

    `split_filter` ("dev"/"test") narrows to one half of a split gold set.
    Asking for a split the file doesn't carry is an error rather than an
    empty run: silently scoring zero queries (or, worse, all of them) is how
    a headline number ends up measured on the wrong population."""
    if split_filter is not None:
        if split_filter not in SPLITS:
            raise ValueError(f"split_filter must be one of {sorted(SPLITS)}, got {split_filter!r}")
        if not golden.has_split():
            raise ValueError(f"golden set {golden.golden_version!r} carries no dev/test split")
    out = []
    for q in golden.queries:
        if query_filter is not None and q.id not in query_filter:
            continue
        if type_filter is not None and not (type_filter & set(q.types)):
            continue
        if split_filter is not None and q.split != split_filter:
            continue
        out.append(q)
    return out


def _steps_to_dicts(steps: Iterable[Any]) -> list[dict]:
    out: list[dict] = []
    for step in steps:
        if dataclasses.is_dataclass(step) and not isinstance(step, type):
            out.append(dataclasses.asdict(step))
        elif isinstance(step, dict):
            out.append(dict(step))
        elif hasattr(step, "to_dict"):
            out.append(step.to_dict())
        else:
            out.append({"repr": repr(step)})
    return out


async def _run_one(
    q: GoldenQuery,
    *,
    run_id: str,
    retrieve_fn: RetrieveFn,
    store: PipelineStore,
    provider: ChatProvider | None,
    project_id: str | None,
    limit: int,
    k_values: Sequence[int],
    strategies_override: list[str] | None,
    use_llm_constraints: bool,
) -> EvalRunQuery:
    row = EvalRunQuery(
        run_id=run_id, query_id=q.id, query_text=q.query, primary_type=q.primary_type, types=list(q.types)
    )
    started = time.perf_counter()
    try:
        result = await retrieve_fn(
            q.query,
            store=store,
            provider=provider,
            project_id=project_id,
            now=q.as_of,
            limit=effective_limit(q, limit),
            use_llm_constraints=use_llm_constraints,
            strategies_override=strategies_override,
        )
        row.ranked_document_ids = list(result.document_ids)
        row.scores = [float(s) for s in result.scores]
        row.predicted_count = result.predicted_count
        row.strategies = list(result.strategies)
        row.steps = _steps_to_dicts(result.steps)
        row.elapsed_ms = float(result.elapsed_ms)
        row.metrics = score_query(
            row.ranked_document_ids,
            q.expected,
            k_values,
            predicted_count=row.predicted_count,
            scores=row.scores or None,
        )
    except Exception as exc:  # noqa: BLE001 - one query's failure must not abort the run
        row.error = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        row.elapsed_ms = (time.perf_counter() - started) * 1000.0
    return row


async def run_ir_eval(
    golden: GoldenSet,
    *,
    store: PipelineStore,
    eval_store: EvalStore | None,
    golden_path: str = "",
    provider: ChatProvider | None = None,
    provider_name: str | None = None,
    model_name: str | None = None,
    project_id: str | None = None,
    limit: int = 10,
    k_values: Sequence[int] = DEFAULT_K_VALUES,
    strategies_override: list[str] | None = None,
    use_llm_constraints: bool = True,
    label: str | None = None,
    retrieve_fn: RetrieveFn | None = None,
    query_filter: set[str] | None = None,
    type_filter: set[str] | None = None,
    split_filter: str | None = None,
    concurrency: int = 4,
    content_hashes: Mapping[str, str] | None = None,
) -> EvalRun:
    """Score every (selected) golden query and return the finished
    `EvalRun` with `queries` attached. Persisted through `eval_store` when
    one is given (row written at start and again at finish); `None` keeps
    the run in memory only (`eval-ir --no-store`).

    `content_hashes` lets a caller (tests) supply the staleness input
    directly; otherwise it is fetched from `eval_store`, and skipped when
    there is no store at all."""
    fn = retrieve_fn if retrieve_fn is not None else default_retrieve_fn()
    provenance = current_code_provenance()
    effective_project = golden.project_id or project_id
    queries = select_queries(golden, query_filter, type_filter, split_filter)
    config = {
        "provider": provider_name,
        "model": model_name,
        "limit": limit,
        "strategies_override": list(strategies_override) if strategies_override else None,
        "use_llm_constraints": use_llm_constraints,
        "k_values": list(k_values),
        # 분할이 config에 들어가야 dev run과 test run의 config_hash가 갈린다 -
        # 같아 버리면 `compare_runs`가 "같은 설정끼리 비교 = run-to-run 노이즈"
        # 경고를 내며 서로 다른 모집단을 짝지으려 든다.
        "split": split_filter,
    }
    run = EvalRun(
        run_id=uuid.uuid4().hex,
        golden_version=golden.golden_version,
        golden_path=golden_path,
        project_id=effective_project,
        config=config,
        config_hash=config_hash(config),
        index_snapshot={},
        git_commit=provenance.identity,
        dirty_files=provenance.dirty_files or None,
        label=label,
        started_at=datetime.now(timezone.utc),
    )

    if eval_store is not None:
        run.index_snapshot = await eval_store.snapshot_index(effective_project)
        if content_hashes is None:
            wanted = sorted({d for q in queries for d in q.expected.document_ids})
            content_hashes = await eval_store.content_hashes_for(wanted)
        await eval_store.save_eval_run(run)

    stale: list[dict] = []
    if content_hashes is not None:
        subset = golden.model_copy(update={"queries": queries})
        stale = [
            {"query_id": i.query_id, "reason": i.reason, "document_ids": list(i.document_ids)}
            for i in check_staleness(subset, content_hashes)
        ]

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _guarded(q: GoldenQuery) -> EvalRunQuery:
        async with semaphore:
            return await _run_one(
                q,
                run_id=run.run_id,
                retrieve_fn=fn,
                store=store,
                provider=provider,
                project_id=effective_project,
                limit=limit,
                k_values=k_values,
                strategies_override=strategies_override,
                use_llm_constraints=use_llm_constraints,
            )

    rows = list(await asyncio.gather(*(_guarded(q) for q in queries)))
    run.queries = rows

    scored: list[QueryScore] = [{"id": r.query_id, "metrics": r.metrics} for r in rows if r.error is None]
    summary = aggregate(scored, golden.types_by_id())
    summary["n_selected"] = len(rows)
    summary["errors"] = [{"query_id": r.query_id, "error": r.error} for r in rows if r.error is not None]
    summary["stale_queries"] = stale
    summary["staleness_checked"] = content_hashes is not None
    run.summary = summary
    run.finished_at = datetime.now(timezone.utc)

    if eval_store is not None:
        await eval_store.save_eval_run_queries(run.run_id, rows)
        await eval_store.save_eval_run(run)
    return run


def run_to_dict(run: EvalRun) -> dict:
    """JSON-ready dump including the (otherwise excluded) query rows - the
    shape `eval-ir --json` writes."""
    payload = run.model_dump(mode="json")
    payload["queries"] = [q.model_dump(mode="json") for q in run.queries]
    return payload
