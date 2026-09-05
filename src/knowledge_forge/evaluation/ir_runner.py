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
import logging
import math
import os
import statistics
import subprocess
import time
import traceback
import uuid
from collections.abc import Awaitable, Iterable, Mapping, Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol

from knowledge_forge.evaluation.golden import SPLITS, GoldenQuery, GoldenSet, check_staleness
from knowledge_forge.evaluation.ir_metrics import DEFAULT_K_VALUES, QueryScore, aggregate, score_query
from knowledge_forge.storage.eval_store import EvalRun, EvalRunQuery, EvalStore, SearchLog

if TYPE_CHECKING:
    from knowledge_forge.chat_provider import ChatProvider
    from knowledge_forge.storage import PipelineStore

logger = logging.getLogger(__name__)

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
    enough to group runs by settings, short enough to print in a listing.

    `config`에는 CLI 인자뿐 아니라 `ranking_settings()`(=랭킹을 실제로 바꾸는
    KF_ 노브들)가 `settings` 키로 들어 있어야 한다 - 아래 주석 참고."""
    canonical = json.dumps(config, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


# --- 랭킹을 바꾸는 설정 (config_hash의 절반) ----------------------------
#
# config_hash가 CLI 인자만 해싱하던 동안, `KF_VECTOR_MIN_SCORE`를 0.42에서 0.50으로
# 바꿔 돌린 run과 원래 run은 **같은 해시**를 달고 나란히 쌓였다. 실제로 지난 세션의
# 스윕 11회(raredf-3 … rare-term-gate-default)가 전부 config_hash `54d9a917f28c`다.
# 그 표만 보고는 (a) 어느 행이 어느 설정이었는지, (b) 그래서 채택한 값이 무엇이었는지
# 복원할 수 없다 - `--label`에 사람이 손으로 박아 둔 문자열이 유일한 기록이었고,
# 라벨은 오타가 나도 아무도 모른다.
#
# 그래서 두 가지를 같이 한다:
#   1) 아래 필드들을 실제로 읽어 `config["settings"]`에 넣는다 → 해시가 달라진다.
#   2) **원본 key/value를 그대로** 남긴다. 해시만 있으면 "달랐다"는 알아도 "무엇이
#      달랐는지"는 모른다(dirty_files를 따로 둔 것과 같은 이유, 커밋 2c29fb0).
#
# 목록은 화이트리스트다. `Settings`를 통째로 넣으면 자격증명(embedding_api_key,
# slack_bot_token, …)이 eval_run에 평문으로 쌓이고, 랭킹과 무관한 값(redis_url,
# data_dir)이 바뀔 때마다 해시가 흔들려 "설정이 달랐다" 경고가 늑대소년이 된다.
#
# 화이트리스트의 위험은 반대쪽이다 - 새 노브가 생겼는데 여기 추가하는 걸 잊으면
# 정확히 이 버그가 재발한다. 그래서 `tests/evaluation/test_eval_provenance.py`가
# `src/knowledge_forge/search/` 전체를 훑어 `settings.<이름>` 참조를 모으고, 아래
# 두 집합 어디에도 없으면 실패한다. 노브를 추가하는 쪽이 여기를 갱신하게 된다.
RANKING_SETTING_FIELDS: tuple[str, ...] = (
    # search/retrieve.py가 랭킹 계산 중에 직접 읽는 값
    "vector_min_score",
    "term_coverage_ratio",
    "term_coverage_min",
    "term_rare_df_max",
    "latest_relevance_pool",
    # 질의 임베딩이 놓이는 벡터 공간의 신원. 모델 이름은 사실상 파티션 키라
    # (`search/service.py::vector_search`의 docstring) 한 글자만 달라도 벡터 arm이
    # 에러 없이 0히트가 된다 - 그 run과 정상 run이 같은 해시면 안 된다.
    "embedding_provider",
    "embedding_model",
    "embedding_dimensions",
    "embedding_base_url",
    # file_search가 읽는 OKF 트리 위치. Postgres 전용 배포에서는 None.
    "okf_dir",
)

# `search/` 안에서 읽히지만 **일부러** 해시에 넣지 않는 설정. 지금은 비어 있고,
# 위 가드 테스트가 "새 노브를 어느 쪽에도 넣지 않는 것"만 막는다. 여기에 넣을 때는
# 왜 랭킹과 무관한지(또는 index_snapshot이 이미 잡는지)를 같이 적어라.
NON_RANKING_SETTING_FIELDS: frozenset[str] = frozenset()


def ranking_settings(source: Any | None = None) -> dict[str, Any]:
    """`RANKING_SETTING_FIELDS`의 현재 값. `source`는 테스트용 주입점이고,
    생략하면 프로세스의 `knowledge_forge.config.settings`를 읽는다."""
    if source is None:
        from knowledge_forge.config import settings  # 지연 임포트 - 설정은 CLI 시작 시점에 로드된다

        source = settings
    return {name: getattr(source, name, None) for name in RANKING_SETTING_FIELDS}


# `config`에 그 키가 아예 없었다는 뜻. `None`(= 그 값이 실제로 None이었다)과 구분해야
# 한다 - 노브를 기록하기 전에 돈 옛 run은 전자이고, "vector_min_score가 None이었다"로
# 읽으면 없는 사실을 지어내는 셈이다.
NOT_RECORDED = "<미기록>"


def settings_diff(config_a: Mapping[str, Any], config_b: Mapping[str, Any]) -> dict[str, list[Any]]:
    """두 run의 `config` 사이에서 **값이 다른 키**만 `{key: [a, b]}`로. `settings`
    하위 dict는 평평하게 펴서 `settings.vector_min_score`처럼 보여준다 - 비교
    경고가 "설정이 다르다"에서 끝나지 않고 *무엇이* 다른지 말하게 하는 부분이다.
    한쪽에 없는 키는 `NOT_RECORDED`로 표시한다."""

    def flat(config: Mapping[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in config.items():
            if key == "settings" and isinstance(value, Mapping):
                out.update({f"settings.{k}": v for k, v in value.items()})
            else:
                out[key] = value
        return out

    a, b = flat(config_a), flat(config_b)
    return {
        key: [a.get(key, NOT_RECORDED), b.get(key, NOT_RECORDED)]
        for key in sorted(set(a) | set(b))
        if a.get(key, NOT_RECORDED) != b.get(key, NOT_RECORDED)
    }


# --- 코퍼스 신원 (index_snapshot) --------------------------------------
#
# `eval_run.index_snapshot`은 처음부터 있었지만 **한 번만, run 시작에** 찍고 아무도
# 다시 보지 않았다. 그 사이에 실제로 이런 일이 났다: 한 세션이 v0을 재는 동안 다른
# 세션이 계속 인제스트해서 263 → 326 → 358 docs, 936 → 1891 → 2856 chunks가 됐고
# **임베딩만 936에 멈춰 있었다**. 결과로 같은 코드가 ndcg@10 0.893 → 0.879,
# none_correct 1.000 → 0.667이 됐다. 어휘 arm은 1891 청크를 보고 벡터 arm은 936만
# 보는 **파이프라인 내부 불일치**였는데 에러도 경고도 없었고, 한 에이전트가 이 하락을
# 자기 코드 변경 탓으로 잘못 귀속했다가 나중에 정정했다.
#
# 그래서 두 가지를 본다:
#   (a) 시작·종료 스냅샷을 둘 다 찍고 다르면 그 run에 플래그를 단다(`changed_during_run`).
#       run이 끝난 뒤에야 알 수 있는 사실이므로 "막는다"가 아니라 "표시한다"이다.
#   (b) 임베딩/청크 정합을 **시작 시점에** 검사하고 어긋나면 크게 경고한다. 이쪽이
#       위 사고의 핵심이었고, run이 시작되기 전에 사람이 멈출 수 있는 유일한 지점이다.
#
# 스키마는 건드리지 않았다 - 전부 기존 `index_snapshot` JSONB 안이다. 상위 키
# (documents/chunks/…)는 **시작 시점 값 그대로** 남겨서 이 코드를 모르는 옛 조회와
# 옛 행이 그대로 살아 있고, 새 정보는 그 옆의 새 키로만 붙는다.
# 두 run이 "같은 코퍼스를 봤는가"를 판정하는 데 쓰는 키들. **행 수만으로는 부족하다**:
# 2026-09-05 실측으로, 다른 세션이 슬랙 문서 197건의 `documents.title`을 다시 쓰자 같은
# 코드의 v0 성적이 0.8516 -> 0.8490으로 움직였는데 행 수도 `updated_at`도 `content_hash`도
# 그대로였다(`check_staleness`도 통과). 그래서 `fingerprints`(랭킹 입력 열들의 집계 해시,
# `eval_store.snapshot_index`)가 같이 들어간다.
CORPUS_IDENTITY_KEYS: tuple[str, ...] = (
    "documents",
    "chunks",
    "knowledge_objects",
    "embeddings_by_kind",
    "embeddings_by_model_and_kind",
    "fingerprints",
)

# 스냅샷 안에서 grain 이름 -> 그 grain의 전체 개수를 담은 키.
_GRAIN_TOTAL_KEY = {"chunk": "chunks", "knowledge_object": "knowledge_objects"}


def corpus_identity(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    """스냅샷에서 **코퍼스 신원 부분만** 뽑는다(규모 + 내용 지문). `index_snapshot`에는
    이제 경고 목록과 종료 스냅샷도 같이 들어 있어서, 두 run의 코퍼스가 같은지는 dict
    통째 비교가 아니라 이 부분집합으로 봐야 한다(옛 행은 상위 키가 곧 이 값이라 그대로
    동작하고, 지문 키만 없다)."""
    if not snapshot:
        return {}
    return {key: snapshot[key] for key in CORPUS_IDENTITY_KEYS if key in snapshot}


def snapshot_drift(before: Mapping[str, Any] | None, after: Mapping[str, Any] | None) -> dict[str, list[Any]]:
    """`{키: [시작값, 종료값]}` - 값이 달라진 코퍼스 신원 항목만. `fingerprints`는
    한 덩어리로 비교하지 않고 지문 이름별로 펴서 `fingerprints.document_titles`처럼
    돌려준다 - "무엇이 달라졌는지"가 md5 두 개보다 훨씬 유용하다."""

    def flat(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in corpus_identity(snapshot).items():
            if key == "fingerprints" and isinstance(value, Mapping):
                out.update({f"fingerprints.{k}": v for k, v in value.items()})
            else:
                out[key] = value
        return out

    a, b = flat(before), flat(after)
    return {key: [a.get(key), b.get(key)] for key in sorted(set(a) | set(b)) if a.get(key) != b.get(key)}


def embedding_consistency(snapshot: Mapping[str, Any] | None, embedding_model: str | None = None) -> dict[str, Any]:
    """"어휘 arm과 벡터 arm이 같은 코퍼스를 보고 있는가"를 판정한다.

    `embedding_model`을 기준으로 세는 게 핵심이다 - `knowledge_object_embeddings`는
    (대상, 모델)마다 한 행이고 검색은 `WHERE embedding_model = %s`로 거르므로
    (`search/service.py::vector_search`), 다른 모델의 벡터가 아무리 많아도 이번 run의
    벡터 arm에는 0행이다. 모델 단위로 세지 않으면 그 상황이 "정합"으로 보인다.

    status: `ok`(활성 모델의 벡터가 모든 grain을 덮는다) / `mismatch`(부분만 덮는다)
    / `missing`(활성 모델의 벡터가 하나도 없다 = 벡터 arm 사망) / `unknown`(모델별
    카운트가 없는 옛 스냅샷)."""
    if not snapshot:
        return {"status": "unknown", "consistent": True, "embedding_model": embedding_model, "grains": {}}
    by_model = snapshot.get("embeddings_by_model_and_kind")
    if not isinstance(by_model, Mapping):
        # 이 코드 이전에 찍힌 스냅샷 - 모델을 구분할 수 없으므로 판정하지 않는다.
        return {"status": "unknown", "consistent": True, "embedding_model": embedding_model, "grains": {}}
    rows = dict(by_model.get(embedding_model) or {}) if embedding_model is not None else {}
    other_models = {m: sum(kinds.values()) for m, kinds in by_model.items() if m != embedding_model}
    grains: dict[str, dict[str, Any]] = {}
    for kind, total_key in _GRAIN_TOTAL_KEY.items():
        total = int(snapshot.get(total_key) or 0)
        embedded = int(rows.get(kind) or 0)
        if total == 0 and embedded == 0:
            continue
        grains[kind] = {
            "total": total,
            "embedded": embedded,
            "coverage": (embedded / total) if total else None,
        }
    embedded_total = sum(int(v) for v in rows.values())
    if embedded_total == 0:
        status = "missing"
    else:
        # 벡터가 실제로 존재하는 grain만 따진다. `KF_EMBEDDING_SOURCE=chunk`인 코퍼스에
        # KnowledgeObject 임베딩이 0인 건 결함이 아니라 설정이다 - 부분 백필만 잡는다.
        partial = [k for k, g in grains.items() if g["embedded"] and g["embedded"] != g["total"]]
        status = "mismatch" if partial else "ok"
    return {
        "status": status,
        "consistent": status in ("ok", "unknown"),
        "embedding_model": embedding_model,
        "grains": grains,
        "embedded_total": embedded_total,
        "other_models": other_models,
    }


def index_warnings(snapshot: Mapping[str, Any] | None, consistency: Mapping[str, Any]) -> list[str]:
    """run 시작 시점에 사람에게 보여줄 경고들(한국어 한 줄씩)."""
    out: list[str] = []
    status = consistency.get("status")
    model = consistency.get("embedding_model")
    if status == "missing":
        line = (
            f"임베딩 모델 '{model}'의 벡터가 인덱스에 하나도 없다 - 벡터 arm이 통째로 죽은 채 "
            "측정된다(에러도 0히트도 구분되지 않는다)"
        )
        others = consistency.get("other_models") or {}
        if others:
            line += ". 다른 모델의 벡터는 있다: " + ", ".join(f"{m}={n}" for m, n in sorted(others.items()))
        out.append(line)
    elif status == "mismatch":
        detail = ", ".join(
            f"{kind} {g['embedded']}/{g['total']}"
            + (f" ({g['coverage']:.1%})" if g.get("coverage") is not None else "")
            for kind, g in sorted((consistency.get("grains") or {}).items())
            if g.get("embedded")
        )
        out.append(
            f"임베딩/코퍼스 불일치 (모델 '{model}'): {detail}. 어휘 arm은 전체 코퍼스를, 벡터 arm은 "
            "임베딩된 일부만 본다 - 두 arm이 다른 코퍼스를 보는 상태로 잰 점수는 코드 변경의 결과가 "
            "아니다(2026-09-04에 936/1891로 정확히 이 일이 났다). 백필 후 다시 재라"
        )
    if snapshot and not snapshot.get("documents"):
        out.append("인덱스에 문서가 0건이다 - 모든 질의가 빈손이 된다(project_id를 잘못 잡았을 수 있다)")
    return out


def log_warnings(header: str, warnings: Sequence[str]) -> None:
    """`logging`으로 크게 찍는다. CLI는 로깅을 설정하지 않으므로 WARNING은 파이썬
    기본 handler를 타고 stderr로 나간다(`search/retrieve.py`의 경고들과 같은 경로)."""
    if not warnings:
        return
    logger.warning("!" * 78)
    logger.warning("%s", header)
    for w in warnings:
        logger.warning("  - %s", w)
    logger.warning("!" * 78)


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


def latency_summary(rows: Sequence[EvalRunQuery]) -> dict[str, dict[str, float]]:
    """run 전체의 지연 요약. 평균과 함께 **p50/p95**를 낸다 - 에이전트 경로의 지연은
    도구를 몇 번 부르느냐에 좌우돼 꼬리가 길고, 평균 하나로는 "대부분 빠른데 몇 건이
    아주 느리다"와 "전부 고르게 느리다"가 구분되지 않는다.

    값이 하나도 없는 지표는 키 자체를 빼는데, 그래야 파이프라인 run의 리포트에
    `generation_ms: 0`이 찍혀 "생성이 공짜였다"로 읽히지 않는다."""
    out: dict[str, dict[str, float]] = {}
    for name in ("retrieval_ms", "generation_ms", "total_ms"):
        values = sorted(v for v in (getattr(r, name) for r in rows) if v is not None)
        if not values:
            continue
        out[name] = {
            "n": float(len(values)),
            "mean": _mean(values),
            "p50": _quantile(values, 0.50),
            "p95": _quantile(values, 0.95),
            "max": values[-1],
        }
    return out


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    """가장 가까운 순위(nearest-rank) 분위수. 보간하지 않는 이유는 이 값이 항상
    **실제로 관측된 한 질의의 시간**이어야 하기 때문이다 - 그래야 p95를 보고
    `eval_run_query`에서 그 질의를 바로 찾아 열어 볼 수 있다."""
    if not sorted_values:
        raise ValueError("sorted_values is empty")
    index = max(0, min(len(sorted_values) - 1, math.ceil(q * len(sorted_values)) - 1))
    return sorted_values[index]


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _aggregate_repeats(attempts: Sequence[Mapping[str, Any]]) -> tuple[dict[str, float], dict[str, float]]:
    """회차별 metrics를 (평균, 표준편차)로 접는다.

    지표별로 **그 지표를 가진 회차만** 평균한다. 에이전트 경로에서는 회차마다 도구
    호출이 달라 `count_match`가 어떤 회차에는 있고 어떤 회차에는 없다 - 없는 회차를
    0으로 세면 "개수를 틀렸다"와 "개수를 말하지 않았다"가 같은 숫자가 된다.

    표준편차는 모표준편차(n으로 나눔)다. 여기서 3회는 표본이 아니라 "이 질의에서
    관측한 전부"이고, 알고 싶은 것도 모집단 추정이 아니라 **관측된 흔들림**이다."""
    buckets: dict[str, list[float]] = {}
    for attempt in attempts:
        for name, value in (attempt.get("metrics") or {}).items():
            buckets.setdefault(name, []).append(float(value))
    mean = {name: _mean(values) for name, values in buckets.items()}
    std = {
        name: (statistics.pstdev(values) if len(values) > 1 else 0.0)
        for name, values in buckets.items()
    }
    return mean, std


def _mean_optional(attempts: Sequence[Mapping[str, Any]], key: str) -> float | None:
    """회차별 타이밍/카운터의 평균. 아무 회차도 그 값을 안 실었으면 None을 지킨다 -
    0으로 접으면 "측정되지 않음"이 "0ms였다"로 둔갑한다(schema.sql의 같은 경고)."""
    values = [float(a[key]) for a in attempts if a.get(key) is not None]
    return _mean(values) if values else None


async def _judge_attempt(
    q: GoldenQuery, answer: str, evidence: list[dict], judge_provider: ChatProvider
) -> dict[str, float]:
    """답변 품질 지표(`groundedness` / `citation_precision`)와, none 유형의
    `none_correct` 재판정.

    왜 none을 여기서 다시 매기나: 파이프라인 경로의 `none_correct`는 "결과를 안
    돌려줬는가"로 매긴다. 그런데 에이전트는 후보를 훑고 나서 "관련 문서가 없습니다"라고
    **답하는** 게 정상 동작이라, 회수한 문서 수로 매기면 옳게 행동한 에이전트가
    구조적으로 0점이 된다. 그래서 판정을 답변 텍스트로 옮기고, 전용 분류 호출로
    묻는다(`judge_answer_declined` - claims의 공백으로 추론하려다 실측에서 정확히
    반대 결론이 나온 경위가 그 docstring에 있다).

    judge 호출이 실패하면 그 지표를 빼고 돌려준다 - 지어낸 점수를 쓰느니 미채점이
    낫다(`judge_search_log`가 None을 돌려주는 것과 같은 태도). none 유형에서
    미채점이면 `score_query`가 매긴 문서 기반 값이 그대로 남는데, 그건 에이전트에게
    불리한 값이므로 summary의 경고와 함께 읽어야 한다."""
    from knowledge_forge.evaluation.judge import judge_answer_declined, judge_search_log

    log = SearchLog(
        log_id=f"eval:{q.id}",
        query=q.query,
        answer=answer,
        evidence=evidence,
        tool_calls=[],
        latency_ms=0,
        created_at=datetime.now(timezone.utc),
    )
    out: dict[str, float] = {}
    verdict = await judge_search_log(log, judge_provider)
    if verdict is not None:
        out["groundedness"] = verdict.groundedness_score
        if verdict.citation_precision is not None:
            out["citation_precision"] = verdict.citation_precision
    if q.expected.none:
        declined = await judge_answer_declined(q.query, answer, judge_provider)
        if declined is not None:
            out["none_correct"] = 1.0 if declined else 0.0
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
    repeats: int = 1,
    judge_provider: ChatProvider | None = None,
) -> EvalRunQuery:
    """한 질의를 `repeats`회 돌려 한 행으로 접는다.

    회차를 늘려도 행은 하나다(PK는 여전히 `(run_id, query_id)`). 회차를 행으로 펴면
    `--compare`/`aggregate`/`paired_delta`가 전부 "질의당 한 행" 가정 위에 있어서 같이
    고쳐야 하고, 그러면 150개 옛 run과의 비교가 끊긴다. 대신 `metrics`에 평균,
    `metrics_std`에 흔들림, `repeats`에 원본을 남긴다.

    한 회차가 실패해도 나머지로 채점한다. 전부 실패했을 때만 `error`다 - 3회 중
    1회가 API 타임아웃이라고 성공한 2회를 버리면, 에이전트 경로에서는 그런 이유로
    질의가 대량 탈락한다."""
    row = EvalRunQuery(
        run_id=run_id, query_id=q.id, query_text=q.query, primary_type=q.primary_type, types=list(q.types)
    )
    attempts: list[dict[str, Any]] = []
    last_error: str | None = None

    for index in range(max(1, repeats)):
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
        except Exception as exc:  # noqa: BLE001 - one query's failure must not abort the run
            last_error = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            attempts.append(
                {"repeat": index, "error": last_error, "total_ms": (time.perf_counter() - started) * 1000.0}
            )
            continue

        wall_ms = (time.perf_counter() - started) * 1000.0
        ranked = list(result.document_ids)
        scores = [float(s) for s in result.scores]
        metrics = score_query(
            ranked,
            q.expected,
            k_values,
            predicted_count=result.predicted_count,
            scores=scores or None,
        )
        answer = str(getattr(result, "answer", "") or "")
        if judge_provider is not None and answer:
            metrics.update(await _judge_attempt(q, answer, list(getattr(result, "evidence", []) or []), judge_provider))
        attempts.append(
            {
                "repeat": index,
                "metrics": metrics,
                "document_ids": ranked,
                "scores": scores,
                "predicted_count": result.predicted_count,
                "strategies": list(result.strategies),
                "steps": _steps_to_dicts(result.steps),
                "answer": answer,
                "elapsed_ms": float(result.elapsed_ms),
                # 파이프라인 경로에는 생성 단계가 없어 아래 넷이 None이다. retrieval_ms는
                # elapsed_ms(=retrieve() 내부 측정치)를 이어받고, total_ms는 하네스의 벽시계다.
                "retrieval_ms": float(getattr(result, "retrieval_ms", result.elapsed_ms)),
                "generation_ms": getattr(result, "generation_ms", None),
                "total_ms": float(getattr(result, "total_ms", wall_ms)),
                "llm_calls": getattr(result, "llm_calls", None),
                "tool_calls": getattr(result, "tool_calls", None),
            }
        )

    successes = [a for a in attempts if "error" not in a]
    if not successes:
        row.error = last_error
        row.total_ms = _mean_optional(attempts, "total_ms")
        row.elapsed_ms = row.total_ms or 0.0
        row.repeats = attempts if len(attempts) > 1 else None
        return row

    first = successes[0]
    row.ranked_document_ids = list(first["document_ids"])
    row.scores = list(first["scores"])
    row.predicted_count = first["predicted_count"]
    row.strategies = list(first["strategies"])
    row.steps = list(first["steps"])
    row.answer = first["answer"] or None
    row.elapsed_ms = _mean([a["elapsed_ms"] for a in successes])
    row.retrieval_ms = _mean_optional(successes, "retrieval_ms")
    row.generation_ms = _mean_optional(successes, "generation_ms")
    row.total_ms = _mean_optional(successes, "total_ms")
    llm_calls = _mean_optional(successes, "llm_calls")
    tool_calls = _mean_optional(successes, "tool_calls")
    row.llm_calls = round(llm_calls) if llm_calls is not None else None
    row.tool_calls = round(tool_calls) if tool_calls is not None else None

    mean_metrics, std_metrics = _aggregate_repeats(successes)
    row.metrics = mean_metrics
    if len(attempts) > 1:
        row.metrics_std = std_metrics
        row.repeats = attempts
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
    settings_snapshot: Mapping[str, Any] | None = None,
    mode: str = "pipeline",
    repeats: int = 1,
    judge_provider: ChatProvider | None = None,
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
    knobs = ranking_settings() if settings_snapshot is None else dict(settings_snapshot)
    config = {
        "provider": provider_name,
        "model": model_name,
        "limit": limit,
        "strategies_override": list(strategies_override) if strategies_override else None,
        "use_llm_constraints": use_llm_constraints,
        "k_values": list(k_values),
        # 랭킹을 바꾸는 KF_ 노브들. 해시에 들어가는 동시에 원본 값 그대로 남는다
        # (위 RANKING_SETTING_FIELDS의 긴 주석 참고).
        "settings": knobs,
        # 분할이 config에 들어가야 dev run과 test run의 config_hash가 갈린다 -
        # 같아 버리면 `compare_runs`가 "같은 설정끼리 비교 = run-to-run 노이즈"
        # 경고를 내며 서로 다른 모집단을 짝지으려 든다.
        "split": split_filter,
        # 같은 이유로 경로와 회차도 해시에 들어간다. pipeline run과 agent run은
        # 애초에 다른 것을 재므로 config_hash가 같으면 절대 안 된다.
        "mode": mode,
        "repeats": repeats,
        "judge": judge_provider is not None,
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

    started_snapshot: dict | None = None
    if eval_store is not None:
        started_snapshot = await eval_store.snapshot_index(effective_project)
        consistency = embedding_consistency(started_snapshot, knobs.get("embedding_model"))
        warnings = index_warnings(started_snapshot, consistency)
        # 상위 키는 시작 시점 카운트 그대로(하위호환), 새 정보는 그 옆에 붙인다.
        run.index_snapshot = {**started_snapshot, "embedding_coverage": consistency, "warnings": warnings}
        log_warnings(
            f"eval-ir: 인덱스 상태 경고 - 이 상태로 잰 점수는 코드 변경의 결과가 아닐 수 있다 (run_id={run.run_id})",
            warnings,
        )
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
                repeats=repeats,
                judge_provider=judge_provider,
            )

    rows = list(await asyncio.gather(*(_guarded(q) for q in queries)))
    run.queries = rows

    scored: list[QueryScore] = [{"id": r.query_id, "metrics": r.metrics} for r in rows if r.error is None]
    summary = aggregate(scored, golden.types_by_id())
    summary["n_selected"] = len(rows)
    summary["errors"] = [{"query_id": r.query_id, "error": r.error} for r in rows if r.error is not None]
    summary["stale_queries"] = stale
    summary["staleness_checked"] = content_hashes is not None
    summary["mode"] = mode
    summary["repeats"] = repeats
    summary["latency"] = latency_summary(rows)
    if mode == "agent" and judge_provider is None:
        # none 유형은 답변으로만 공정하게 채점된다(`_judge_attempt`의 긴 주석).
        # judge 없이 돌면 그 6+6건이 "후보를 훑었다"는 이유로 0점이 되고, 그 0이
        # 파이프라인 run과의 비교에서 경로 차이로 잘못 읽힌다.
        summary["mode_warnings"] = [
            "--mode agent를 --judge 없이 돌렸다: none/negative 유형의 none_correct는 "
            "회수한 문서 수로 매겨져 에이전트 경로에 구조적으로 불리하다"
        ]
    run.finished_at = datetime.now(timezone.utc)

    if eval_store is not None:
        # 종료 스냅샷 - 시작과 다르면 이 run이 도는 동안 코퍼스가 움직였다는 뜻이고,
        # 그러면 질의 76개가 서로 다른 인덱스를 본 셈이라 run 내부부터 이미 섞여 있다.
        finished_snapshot = await eval_store.snapshot_index(effective_project)
        drift = snapshot_drift(started_snapshot, finished_snapshot)
        run_warnings = list(run.index_snapshot.get("warnings") or [])
        if drift:
            run_warnings.append(
                "run이 도는 동안 코퍼스가 변했다: "
                + ", ".join(f"{k} {before!r} -> {after!r}" for k, (before, after) in drift.items())
                + " - 질의마다 다른 인덱스를 본 run이라 다른 run과의 비교는 물론 이 run 내부의 "
                "유형별 비교도 신뢰할 수 없다"
            )
        run.index_snapshot = {
            **run.index_snapshot,
            "finished": finished_snapshot,
            "changed_during_run": bool(drift),
            "drift": drift,
            "warnings": run_warnings,
        }
        if drift:
            log_warnings(f"eval-ir: 측정 도중 코퍼스가 변했다 (run_id={run.run_id})", run_warnings[-1:])
        summary["index_warnings"] = run_warnings

    run.summary = summary

    if eval_store is not None:
        await eval_store.save_eval_run_queries(run.run_id, rows)
        await eval_store.save_eval_run(run)
    return run


def run_to_dict(run: EvalRun) -> dict:
    """JSON-ready dump including the (otherwise excluded) query rows - the
    shape `eval-ir --json` writes."""
    payload = run.model_dump(mode="json")
    # created_at/updated_at은 DB가 채우는 값이라 방금 돈 run의 메모리 객체에는
    # 항상 None이다. 항상 null인 키를 파일에 남기면 "기록 시각이 없다"로 잘못
    # 읽히므로 아예 뺀다 - 이 run의 시각은 started_at/finished_at이다.
    payload["queries"] = [q.model_dump(mode="json", exclude={"created_at", "updated_at"}) for q in run.queries]
    return payload
