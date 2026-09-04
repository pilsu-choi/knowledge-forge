"""Paired A/B comparison of two `EvalRun`s (wiki/search-evaluation.md (C)).

Why *paired*: two runs over the same gold file score the same queries, so
the per-query difference B - A is the signal and the between-query
variance (some queries are just hard) cancels out. Reporting only the two
overall means would hide a 0.03 gain inside a 0.2 spread. The bootstrap
CI over those deltas (seeded, so the report is reproducible) is the "is
this real?" check; win/loss/tie is the human-readable version.

Comparability rules, enforced here rather than trusted to the caller:

- different `golden_version` -> `ValueError`. The queries are not the same
  population, so a paired delta is meaningless.
- different `index_snapshot` -> allowed but flagged (`snapshot_mismatch`,
  `warnings`). A re-ingested index changes what *can* be retrieved, so the
  delta may be drift rather than a retrieval change; the caller decides.
  코퍼스 비교는 dict 통째가 아니라 `corpus_identity()`(규모 + 내용 지문)로 한다 -
  `index_snapshot`에는 이제 경고 목록·종료 스냅샷도 같이 들어 있기 때문이다.
  **행 수만 비교하면 부족하다**: 다른 세션이 문서 197건의 title을 다시 쓰자 같은
  코드의 v0가 0.8516 -> 0.8490으로 움직였는데 행 수는 그대로였다(2026-09-05).
- run **도중에** 코퍼스가 변한 run(`changed_during_run`), 임베딩/청크가 어긋난
  상태로 잰 run(`embedding_coverage.status != ok`)도 각각 경고한다. 전자는 그
  run 내부부터 이미 섞여 있고, 후자는 어휘 arm과 벡터 arm이 다른 코퍼스를 본
  상태라 "코드 변경의 결과"가 아니다(2026-09-04 사고).
- 설정이 다르면 **무엇이** 다른지 적는다(`config_diff`). config_hash는 "달랐다"만
  말한다 - 노브 값까지 `config["settings"]`에 남기는 이유가 이것이다.
- `--strategies` override는 후보 생성뿐 아니라 **후처리 전략까지 통째로** 교체한다
  (override run에는 `count_match`가 아예 없다). 그래서 한쪽만 override면 "arm 둘"이
  아니라 "파이프라인 둘"을 비교하는 것이고, 이건 조용히 넘어가면 안 되는 종류다.
- **코드·설정·코퍼스가 모두 같은 두 run**(`reproducibility.identical_setup`)은 A/B가
  아니라 재현성 측정이다. 그 delta는 정의상 전부 노이즈이고, 동시에 이 파이프라인의
  **노이즈 바닥**이다 - 다른 A/B의 delta가 이보다 작으면 의미가 없다. 실제로 그런
  비교를 "개선을 쟀다"로 읽은 사고가 있었다.
- queries with an `error` in either run (empty metrics) are simply not
  shared and drop out of the pairing - `n` says how many survived.
- 코드 프로버넌스(`git_commit`)는 비교를 막지는 않지만 **반드시 보인다**.
  dirty 작업 트리에서 잰 run은 기록된 커밋만으로 되살릴 수 없고, base commit이
  같은데 코드 상태가 다른 두 run을 "같은 커밋 = 같은 코드"로 읽는 게 정확히
  이 표 하나를 못 믿게 만든 사고였다(b470057 밑에 0.736과 0.477이 나란히).
  그래서 조용히 넘어가지 않고 `warnings`/`provenance_mismatch`로 올린다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from knowledge_forge.evaluation.ir_metrics import bootstrap_ci, paired_delta
from knowledge_forge.evaluation.ir_runner import base_commit, corpus_identity, is_dirty_commit, settings_diff
from knowledge_forge.storage.eval_store import EvalRun, EvalRunQuery


def _metrics_by_query(rows: Sequence[EvalRunQuery]) -> dict[str, dict[str, float]]:
    return {r.query_id: dict(r.metrics) for r in rows if r.error is None and r.metrics}


def _provenance_warnings(run_a: EvalRun, run_b: EvalRun) -> tuple[list[str], bool]:
    """(경고 목록, provenance_mismatch). mismatch는 "base commit은 같은데 실제로
    돈 코드는 다르다" - 커밋 해시만 보고 짝지으면 안 되는 그 경우다."""
    out: list[str] = []
    for name, run in (("A", run_a), ("B", run_b)):
        if is_dirty_commit(run.git_commit):
            n = len(run.dirty_files or [])
            out.append(
                f"run {name}({run.run_id})는 수정된 작업 트리에서 측정됐다 "
                f"(git_commit={run.git_commit}, 변경 파일 {n}개) - 기록된 커밋을 체크아웃해도 그 코드가 아니다"
            )
    mismatch = base_commit(run_a.git_commit) == base_commit(run_b.git_commit) and run_a.git_commit != run_b.git_commit
    if mismatch:
        out.append(
            "두 run의 base commit은 같지만 실제로 돈 코드가 다르다 - 커밋 해시만 보고 '같은 코드'로 읽으면 안 된다"
        )
    return out, mismatch


def _identity_detail(key: str, a: Any, b: Any) -> str:
    """차이 한 줄. 지문은 md5 두 개를 통째로 찍는 대신 **어떤 지문이** 달라졌는지를
    말한다 - 사람이 읽고 행동할 수 있는 건 이름 쪽이다."""
    if key == "fingerprints" and isinstance(a, Mapping) and isinstance(b, Mapping):
        names = sorted(k for k in set(a) & set(b) if a[k] != b[k])
        rendered = ", ".join(f"{n}({str(a[n])[:8]}… vs {str(b[n])[:8]}…)" for n in names)
        return f"내용 지문이 다르다: {rendered or '비교 가능한 지문 없음'}"
    return f"{key}: {a!r} vs {b!r}"


def _reproducibility(head: Mapping[str, Any] | None, *, replication: bool) -> dict[str, Any]:
    """헤드라인 지표의 "얼마나 움직였나". `identical_setup`이 True면 이 숫자들이 곧
    **재현 노이즈 바닥**이다 - 코드도 설정도 코퍼스도 같은 두 런의 delta는 정의상
    전부 노이즈이기 때문이다."""
    deltas = list(head.get("deltas") or []) if head else []
    moved = [d for d in deltas if d != 0]
    return {
        "identical_setup": replication,
        "n": len(deltas),
        "moved": len(moved),
        "mean_abs_delta": (sum(abs(d) for d in moved) / len(moved)) if moved else 0.0,
        "max_abs_delta": max((abs(d) for d in moved), default=0.0),
    }


def _replication_warning(run_a: EvalRun, repro: Mapping[str, Any]) -> str:
    """코드·설정·코퍼스가 모두 같은 두 런을 비교하고 있다.

    두 가지 이유로 조용히 넘어가면 안 된다. (1) 그런 비교를 "개선을 쟀다"로 읽는
    실수가 실제로 있었다. (2) 그 delta가 바로 **이 파이프라인의 재현 노이즈 바닥**
    이고, 다른 A/B의 delta가 이보다 작으면 의미가 없다.

    2026-09-05 실측 두 건이 왜 이 바닥이 0이 아닌지를 보여 준다.
    (a) 코드가 완전히 같은 두 baseline이 76질의 중 8개에서 갈렸다(최대 ±0.63):
        `storage/postgres_store.py`의 벡터 검색이 `ORDER BY 거리 LIMIT n`뿐이라
        결정적 tiebreak가 없고, 임베딩 2856행 중 508행(17.8%)이 벡터가 완전히 같은
        중복 그룹이라 LIMIT 경계에서 문서 랭킹이 런마다 바뀐다(SQL 수정은 별도 브랜치).
    (b) 카운트가 똑같은데도(358/2856/2856) 세 시간 사이에 `v0-sequence-001`이
        0.177 -> 0.000으로 굳어졌다. 이건 churn이 아니라 `index_snapshot`이 못 잡는
        **코퍼스 내용 변화**다 - 카운트 스냅샷의 한계이고, 그래서 이 경고는 두
        가능성을 같이 말한다."""
    if repro["moved"] == 0:
        return (
            f"두 런은 코드·설정·코퍼스가 모두 같다 (code={run_a.git_commit}, cfg={run_a.config_hash}) - "
            f"이 비교는 A/B가 아니라 재현성 측정이다. 공통 {repro['n']}개 질의가 하나도 움직이지 않았다"
            "(이번 짝짓기에서는 노이즈 바닥 0)"
        )
    return (
        f"두 런은 코드·설정·코퍼스가 모두 같다 (code={run_a.git_commit}, cfg={run_a.config_hash}) - "
        f"이 delta는 코드/설정의 효과가 아니라 재현 노이즈이거나 카운트가 안 변한 코퍼스 내용 변화다. "
        f"관측된 churn: 공통 {repro['n']}개 중 {repro['moved']}개 질의가 움직였고 평균 |delta| "
        f"{repro['mean_abs_delta']:.3f}, 최대 {repro['max_abs_delta']:.3f} - 다른 A/B의 delta가 이보다 "
        "작으면 이 바닥과 구분되지 않는다"
    )


def _index_health_warnings(run_a: EvalRun, run_b: EvalRun) -> list[str]:
    """각 run이 **어떤 상태의 인덱스에서** 측정됐는지. run 시작 때도 경고했지만,
    비교는 몇 시간·며칠 뒤에 다른 사람이 하는 일이라 그 자리에서 다시 보여야 한다."""
    out: list[str] = []
    for name, run in (("A", run_a), ("B", run_b)):
        snapshot = run.index_snapshot or {}
        if snapshot.get("changed_during_run"):
            drift = snapshot.get("drift") or {}
            detail = ", ".join(f"{k} {v[0]!r} -> {v[1]!r}" for k, v in drift.items())
            out.append(f"run {name}는 측정 도중 코퍼스가 변했다 ({detail}) - 질의마다 다른 인덱스를 봤다")
        coverage = snapshot.get("embedding_coverage") or {}
        status = coverage.get("status")
        if status in ("mismatch", "missing"):
            grains = ", ".join(
                f"{kind} {g.get('embedded')}/{g.get('total')}" for kind, g in sorted((coverage.get("grains") or {}).items())
            )
            out.append(
                f"run {name}는 임베딩/코퍼스가 어긋난 상태에서 측정됐다 (status={status}, "
                f"모델={coverage.get('embedding_model')!r}, {grains}) - 어휘 arm과 벡터 arm이 다른 코퍼스를 봤다"
            )
    return out


def _pipeline_warnings(
    run_a: EvalRun, queries_a: Sequence[EvalRunQuery], run_b: EvalRun, queries_b: Sequence[EvalRunQuery]
) -> list[str]:
    """`--strategies` override가 걸린 run은 "arm 하나만 켠 같은 파이프라인"이 아니라
    **다른 파이프라인**이다: override는 후보 생성 전략뿐 아니라 후처리 전략(count/
    latest/…)까지 통째로 갈아치우므로, override run에는 `count_match`가 아예 없고
    exhaustive/count 질의는 다른 규칙으로 채점된다. 그런 run과 기본 run의 델타는
    "arm의 기여도"가 아니라 "파이프라인 둘의 차이"다."""
    out: list[str] = []
    ov_a = run_a.config.get("strategies_override")
    ov_b = run_b.config.get("strategies_override")
    if ov_a != ov_b:
        out.append(
            f"strategies_override가 다르다 (A={ov_a!r}, B={ov_b!r}) - override는 후보 생성뿐 아니라 후처리 "
            "전략까지 통째로 교체하므로 이 비교는 'arm 둘'이 아니라 '파이프라인 둘'의 비교다"
        )
    ids_a = {r.query_id for r in queries_a if r.error is None and r.metrics}
    ids_b = {r.query_id for r in queries_b if r.error is None and r.metrics}
    if ids_a != ids_b:
        # 페어드 비교는 교집합만 쓴다(`n`이 그 크기다). 한쪽이 `--query-id`/`--type`으로
        # 일부만 돌았거나 질의가 실패했으면, 두 run의 "전체 평균"은 애초에 다른
        # 모집단의 평균이라 표의 overall끼리 비교하면 안 된다.
        out.append(
            f"채점된 질의 집합이 다르다 (A {len(ids_a)}개, B {len(ids_b)}개, 공통 {len(ids_a & ids_b)}개) - "
            "델타는 공통 질의만의 것이고, 두 run의 overall 평균끼리는 비교할 수 없다"
        )
    else:
        names_a = {m for r in queries_a for m in r.metrics}
        names_b = {m for r in queries_b for m in r.metrics}
        only_a, only_b = sorted(names_a - names_b), sorted(names_b - names_a)
        if only_a or only_b:
            out.append(
                f"같은 질의인데 한쪽에만 있는 지표가 있다 (A만: {only_a or '-'}, B만: {only_b or '-'}) - "
                "두 run이 같은 규칙으로 채점되지 않았다는 증거다(대개 strategies_override가 후처리 전략까지 "
                "갈아치웠기 때문이다)"
            )
    return out


def _win_loss_tie(deltas: Sequence[float]) -> dict[str, int]:
    return {
        "wins": sum(1 for d in deltas if d > 0),
        "losses": sum(1 for d in deltas if d < 0),
        "ties": sum(1 for d in deltas if d == 0),
    }


def compare_runs(
    run_a: EvalRun,
    queries_a: Sequence[EvalRunQuery],
    run_b: EvalRun,
    queries_b: Sequence[EvalRunQuery],
    *,
    metric: str = "ndcg@10",
    n_boot: int = 1000,
    seed: int = 0,
) -> dict:
    """B relative to A. Returns a JSON-ready dict: the headline `metric`
    with mean/CI/win-loss-tie/per-query/per-type breakdown, plus a
    `per_metric` table of mean deltas over every metric both runs share."""
    if run_a.golden_version != run_b.golden_version:
        raise ValueError(
            f"cannot compare runs on different golden versions: {run_a.golden_version!r} vs {run_b.golden_version!r}"
        )
    warnings: list[str] = []
    id_a, id_b = corpus_identity(run_a.index_snapshot), corpus_identity(run_b.index_snapshot)
    # **양쪽에 다 있는 키만** 비교한다. 한쪽에만 있는 키는 스냅샷 포맷이 늘어난
    # 것이지 코퍼스가 달랐다는 증거가 아니다 - 옛 행에는 `embeddings_by_model_and_kind`나
    # `fingerprints`가 없고, 그걸 차이로 세면 옛 run과의 모든 비교에 늑대소년 경고가 붙는다.
    differing = sorted(key for key in set(id_a) & set(id_b) if id_a[key] != id_b[key])
    snapshot_mismatch = bool(differing)
    if snapshot_mismatch:
        detail = ", ".join(_identity_detail(key, id_a[key], id_b[key]) for key in differing)
        counts_same = "fingerprints" in differing and len(differing) == 1
        warnings.append(
            f"두 run이 서로 다른 코퍼스에서 측정됐다 (index_snapshot: {detail}) - 델타가 코드/설정이 아니라 "
            "인덱스 변화일 수 있다"
            + (
                ". 행 수는 전부 같고 **내용만** 달라졌다 - 적재 후에 파생 열(예: documents.title)이 "
                "다시 쓰인 모양이고, 그건 updated_at/content_hash/check_staleness 어디에도 안 잡힌다"
                if counts_same
                else ""
            )
        )
    warnings.extend(_index_health_warnings(run_a, run_b))
    config_diff = settings_diff(run_a.config, run_b.config)
    # "코드도 설정도 코퍼스도 같다" = 정의상 delta가 전부 재현 노이즈여야 하는 짝짓기.
    # git_commit이 None(비-git 체크아웃, 또는 옛 행)이면 "같다"고 주장할 수 없다.
    same_code = run_a.git_commit is not None and run_a.git_commit == run_b.git_commit
    same_config = run_a.config_hash == run_b.config_hash
    replication = same_code and same_config and not snapshot_mismatch
    if config_diff:
        # config_hash가 다르다는 사실만으로는 아무도 설정을 복원할 수 없다 - 값까지 적는다.
        warnings.append(
            "두 run의 설정이 다르다: "
            + ", ".join(f"{key} {a!r} -> {b!r}" for key, (a, b) in config_diff.items())
        )
    elif not same_config and (run_a.config or run_b.config):
        warnings.append(
            "config_hash는 다른데 기록된 config는 같다 - 한쪽이 노브를 기록하기 전에 돈 옛 run일 수 있다"
        )
    if same_config and same_code and snapshot_mismatch:
        warnings.append(
            "코드와 설정이 같고 코퍼스만 다르다 - 이 delta는 인덱스 변화와 재현 노이즈의 합이지 "
            "코드/설정의 효과가 아니다"
        )
    elif same_config and (run_a.git_commit is None or run_b.git_commit is None):
        # 설정은 같은데 코드가 같았는지 알 수 없다(옛 행/비-git). 설정 A/B는 아니라는
        # 것만 말할 수 있다. 코드가 **다른** 건 정상적인 코드 A/B라 경고하지 않는다.
        warnings.append(
            "두 run의 config_hash가 같다 - 설정 차이를 잰 비교가 아니다(코드가 같았는지는 "
            "git_commit이 없어 알 수 없다)"
        )
    if run_a.golden_path != run_b.golden_path:
        warnings.append(
            f"골든셋 파일 경로가 다르다 ({run_a.golden_path!r} vs {run_b.golden_path!r}) - golden_version이 같아도 "
            "파일 내용이 같다는 보장은 없다"
        )
    warnings.extend(_pipeline_warnings(run_a, queries_a, run_b, queries_b))
    prov_warnings, provenance_mismatch = _provenance_warnings(run_a, run_b)
    warnings.extend(prov_warnings)

    a = _metrics_by_query(queries_a)
    b = _metrics_by_query(queries_b)
    deltas_all = paired_delta(a, b)
    reproducibility = _reproducibility(deltas_all.get(metric), replication=replication)
    if replication:
        warnings.append(_replication_warning(run_a, reproducibility))
    per_metric = {
        name: {k: v for k, v in d.items() if k in ("n", "mean_delta", "wins", "losses", "ties")}
        for name, d in deltas_all.items()
    }

    out: dict = {
        "metric": metric,
        "golden_version": run_a.golden_version,
        "run_a": {
            "run_id": run_a.run_id,
            "label": run_a.label,
            "config_hash": run_a.config_hash,
            "git_commit": run_a.git_commit,
            "dirty": is_dirty_commit(run_a.git_commit),
        },
        "run_b": {
            "run_id": run_b.run_id,
            "label": run_b.label,
            "config_hash": run_b.config_hash,
            "git_commit": run_b.git_commit,
            "dirty": is_dirty_commit(run_b.git_commit),
        },
        "snapshot_mismatch": snapshot_mismatch,
        "provenance_mismatch": provenance_mismatch,
        "config_diff": config_diff,
        "reproducibility": reproducibility,
        "warnings": warnings,
        "n_boot": n_boot,
        "seed": seed,
        "per_metric": per_metric,
    }
    head = deltas_all.get(metric)
    if head is None or head["n"] == 0:
        out.update({"n": 0, "mean_a": None, "mean_b": None, "mean_delta": None, "ci95": None})
        out.update({"wins": 0, "losses": 0, "ties": 0, "per_query": [], "by_type": {}})
        return out

    ids: list[str] = head["query_ids"]
    deltas: list[float] = head["deltas"]
    lo, hi = bootstrap_ci(deltas, n_boot=n_boot, seed=seed)
    types_by_id = {r.query_id: list(r.types) for r in queries_b}
    per_query: list[dict[str, Any]] = [
        {"query_id": qid, "a": a[qid][metric], "b": b[qid][metric], "delta": d, "types": types_by_id.get(qid, [])}
        for qid, d in zip(ids, deltas, strict=True)
    ]
    grouped: dict[str, list[float]] = {}
    for qid, d in zip(ids, deltas, strict=True):
        for t in types_by_id.get(qid, []):
            grouped.setdefault(t, []).append(d)
    by_type = {
        t: {"n": len(ds), "mean_delta": sum(ds) / len(ds), **_win_loss_tie(ds)} for t, ds in sorted(grouped.items())
    }
    out.update(
        {
            "n": head["n"],
            "mean_a": sum(a[q][metric] for q in ids) / len(ids),
            "mean_b": sum(b[q][metric] for q in ids) / len(ids),
            "mean_delta": head["mean_delta"],
            "ci95": [lo, hi],
            "significant": not (lo <= 0.0 <= hi),
            "wins": head["wins"],
            "losses": head["losses"],
            "ties": head["ties"],
            "per_query": per_query,
            "by_type": by_type,
        }
    )
    return out
