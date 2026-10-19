"""두 실행 결과의 질의별 paired 비교 - delta 평균, bootstrap 95% CI(시드 고정), 승/패/무.

같은 골든셋 버전 + 같은 문서 지문이어야 비교한다(아니면 `EvalError`). 청크·KO·임베딩 지문만 다르면
경고만 한다 - 재청킹/임베딩 교체 A/B가 바로 그 비교이기 때문이다(청크 라벨은 인용문으로 해석된다).
"""

from __future__ import annotations

import random
from typing import Any

from knowledge_forge.exceptions import EvalError

_TIE = 1e-9


def bootstrap_ci(deltas: list[float], *, seed: int = 0, resamples: int = 10_000) -> tuple[float, float]:
    rng = random.Random(seed)
    n = len(deltas)
    means = sorted(sum(rng.choices(deltas, k=n)) / n for _ in range(resamples))
    return means[int(0.025 * resamples)], means[int(0.975 * resamples) - 1]


def compare_runs(a: dict[str, Any], b: dict[str, Any], metric: str = "ndcg@10", *, seed: int = 0, resamples: int = 10_000) -> dict[str, Any]:
    """delta = B - A. 두 실행 모두 ok이고 `metric`이 있는 항목만 짝짓는다."""
    if a["golden"]["version"] != b["golden"]["version"]:
        raise EvalError(f"골든셋 버전이 다르다: {a['golden']['version']} vs {b['golden']['version']}")
    if a["corpus_before"]["documents"] != b["corpus_before"]["documents"]:
        raise EvalError("문서 집합 지문이 다르다 - 같은 코퍼스에서 잰 실행이 아니다")
    warnings = [
        f"{key} 지문이 다르다"
        for key in ("chunks", "knowledge_objects", "embeddings")
        if a["corpus_before"].get(key) != b["corpus_before"].get(key)
    ]
    # none 생성 게이트와 채점 둘 다 `vector_min_score`에 기대는데(생성: 벡터 최고 유사도가 이보다
    # 낮아야 채택, 채점: 결과가 비어야 정답) 두 run의 랭킹 설정이 다르면 none_correct(그리고 다른
    # 지표) delta가 검색 품질이 아니라 이 값 차이를 잰 것일 수 있다.
    settings_a, settings_b = a.get("provenance", {}).get("settings", {}), b.get("provenance", {}).get("settings", {})
    diff = sorted(k for k in settings_a.keys() | settings_b.keys() if settings_a.get(k) != settings_b.get(k))
    if diff:
        note = " - none_correct 등 delta는 검색 품질이 아니라 이 설정 차이를 반영할 수 있다" if "vector_min_score" in diff else ""
        warnings.append(f"provenance.settings 차이: {', '.join(diff)}{note}")
    # 질의 임베딩 고정: 한쪽만 고정이거나 파일이 다르면 임베딩 노이즈가 delta에 섞인다.
    pin_a, pin_b = (run.get("provenance", {}).get("query_embeddings") or {"pinned": False} for run in (a, b))
    if pin_a["pinned"] != pin_b["pinned"]:
        warnings.append("질의 임베딩 고정이 한쪽에만 있다 - 원격 임베딩 노이즈가 delta에 섞일 수 있다")
    elif pin_a["pinned"] and pin_a["sha256"] != pin_b["sha256"]:
        warnings.append("질의 임베딩 고정 파일이 다르다(sha256) - 같은 벡터로 잰 비교가 아니다")
    warnings += [f"{run['run_id']}: {w}" for run in (a, b) for w in run.get("warnings", [])]

    def _scores(run: dict[str, Any]) -> dict[str, float]:
        return {r["id"]: r["metrics"][metric] for r in run["items"] if r["status"] == "ok" and metric in r["metrics"]}

    score_a, score_b = _scores(a), _scores(b)
    ids = sorted(score_a.keys() & score_b.keys())
    if not ids:
        raise EvalError(f"두 실행에 공통으로 '{metric}'이 있는 항목이 없다")
    deltas = [score_b[i] - score_a[i] for i in ids]
    low, high = bootstrap_ci(deltas, seed=seed, resamples=resamples)
    changed = [
        {"id": i, "a": score_a[i], "b": score_b[i], "delta": d}
        for d, i in sorted(zip(deltas, ids, strict=True))
        if abs(d) > _TIE
    ]
    return {
        "metric": metric,
        "a": a["run_id"],
        "b": b["run_id"],
        "n": len(ids),
        "mean_a": sum(score_a[i] for i in ids) / len(ids),
        "mean_b": sum(score_b[i] for i in ids) / len(ids),
        "delta": sum(deltas) / len(ids),
        "ci95": [low, high],
        "wins": sum(d > _TIE for d in deltas),
        "losses": sum(d < -_TIE for d in deltas),
        "ties": sum(abs(d) <= _TIE for d in deltas),
        "warnings": warnings,
        "changed": changed,
    }
