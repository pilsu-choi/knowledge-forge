"""Paired comparison: reproducible bootstrap, refusal rules, CLI wiring."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from knowledge_forge import cli
from knowledge_forge.eval.compare import compare_runs
from knowledge_forge.exceptions import EvalError


def _report(run_id: str, scores: list[float], **overrides) -> dict:
    report = {
        "run_id": run_id,
        "golden": {"version": "chunk-test"},
        "corpus_before": {"documents": {"count": 2, "hash": "d"}, "chunks": {"count": 3, "hash": "c"}, "knowledge_objects": {"count": 0, "hash": "k"}, "embeddings": {"chunk": 3}},
        "warnings": [],
        "items": [{"id": f"q{i}", "status": "ok", "metrics": {"ndcg@10": s}} for i, s in enumerate(scores)]
        + [{"id": "stale", "status": "stale"}],
    }
    report.update(overrides)
    return report


A = [0.2, 0.5, 1.0, 0.0, 0.7, 0.3]
B = [0.4, 0.5, 0.8, 0.6, 0.7, 0.9]


def test_compare_is_reproducible_with_a_seed():
    first = compare_runs(_report("a", A), _report("b", B), seed=7)
    second = compare_runs(_report("a", A), _report("b", B), seed=7)

    assert first == second
    assert first["delta"] == pytest.approx(sum(b - a for a, b in zip(A, B, strict=True)) / len(A))
    assert (first["wins"], first["losses"], first["ties"], first["n"]) == (3, 1, 2, 6)
    assert first["ci95"][0] <= first["delta"] <= first["ci95"][1]
    assert first["changed"][0]["id"] == "q2"


def test_identical_runs_have_zero_delta_and_zero_width_ci():
    result = compare_runs(_report("a", A), _report("b", A))

    assert result["delta"] == 0 and result["ci95"] == [0.0, 0.0] and result["ties"] == 6


def test_compare_refuses_different_golden_or_documents_and_warns_on_chunks():
    with pytest.raises(EvalError):
        compare_runs(_report("a", A), _report("b", B, golden={"version": "other"}))
    changed_docs = _report("b", B)
    changed_docs["corpus_before"]["documents"]["hash"] = "x"
    with pytest.raises(EvalError):
        compare_runs(_report("a", A), changed_docs)

    rechunked = _report("b", B)
    rechunked["corpus_before"]["chunks"]["hash"] = "x"
    assert compare_runs(_report("a", A), rechunked)["warnings"] == ["chunks 지문이 다르다"]


def test_compare_warns_when_provenance_settings_differ():
    """none 게이트와 채점 둘 다 `vector_min_score`를 쓰므로, 두 run의 랭킹 설정이 다르면 delta가
    검색 품질이 아니라 그 값 차이를 반영할 수 있다(wiki/search-quality-review-2026-09-15.md 3.2)."""
    a = _report("a", A, provenance={"settings": {"vector_min_score": 0.45, "embedding_model": "m"}})
    b = _report("b", B, provenance={"settings": {"vector_min_score": 0.0, "embedding_model": "m"}})

    result = compare_runs(a, b)

    assert any("vector_min_score" in w for w in result["warnings"])


def test_compare_is_silent_about_settings_when_provenance_is_absent_or_identical():
    same = _report("b", B, provenance={"settings": {"vector_min_score": 0.45}})
    assert compare_runs(_report("a", A, provenance={"settings": {"vector_min_score": 0.45}}), same)["warnings"] == []
    assert compare_runs(_report("a", A), _report("b", B))["warnings"] == []


def test_compare_cli_prints_delta(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps(_report("a", A)))
    b.write_text(json.dumps(_report("b", A)))

    result = CliRunner().invoke(cli.app, ["eval", "compare", str(a), str(b)])

    assert result.exit_code == 0, result.output
    assert "delta +0.0000" in result.output and "승 0 / 패 0 / 무 6" in result.output


def _pinned(sha: str | None) -> dict:
    return {"provenance": {"query_embeddings": {"pinned": True, "sha256": sha} if sha else {"pinned": False}}}


def test_compare_warns_when_query_embedding_pin_differs():
    one_sided = compare_runs(_report("a", A, **_pinned("x")), _report("b", B, **_pinned(None)))
    assert any("한쪽에만" in w for w in one_sided["warnings"])
    other_file = compare_runs(_report("a", A, **_pinned("x")), _report("b", B, **_pinned("y")))
    assert any("파일이 다르다" in w for w in other_file["warnings"])
    same = compare_runs(_report("a", A, **_pinned("x")), _report("b", B, **_pinned("x")))
    assert not any("질의 임베딩" in w for w in same["warnings"])
