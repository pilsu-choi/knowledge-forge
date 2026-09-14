"""Run-time label resolution, scoring, and the e2e call-order ranking."""

from __future__ import annotations

import asyncio
import math
import unicodedata
from types import SimpleNamespace

import pytest

from knowledge_forge.agent.providers.base import ToolSpec
from knowledge_forge.eval import run
from knowledge_forge.eval.goldenset import Evidence, GoldenItem, GoldenSet, resolve_gold
from knowledge_forge.eval.run import lookup_metrics, run_eval, score_item, summarize

QUOTE = "운영 환경 배포는 9월 30일"


def _lookup(**labels) -> GoldenItem:
    return GoldenItem(id="lookup-000", query="PF 운영 배포일은 언제인가", answer="9월 30일", kind="lookup", split="dev", **labels)


async def test_chunk_labels_follow_the_quote_across_a_rechunk(fakes):
    item = _lookup(evidence=[Evidence(document_id="d1", quote=QUOTE)])
    before = fakes.FakeStore(chunks={"d1": [("c1", f"회의록\n{QUOTE}로 확정"), ("c2", "기타 안건")]})
    # re-chunked: new ids, whitespace changed, text decomposed to NFD
    after = fakes.FakeStore(chunks={"d1": [("n1", "회의록"), ("n2", unicodedata.normalize("NFD", "운영  환경\n배포는 9월 30일로 확정"))]})
    gone = fakes.FakeStore(chunks={"d1": [("n1", "회의록"), ("n2", "배포일 미정")]})

    assert await resolve_gold(item, "chunk", before) == ({"c1"}, None)
    assert await resolve_gold(item, "chunk", after) == ({"n2"}, None)
    assert await resolve_gold(item, "chunk", gone) == (None, "quote_not_found")


async def test_missing_knowledge_object_is_stale(fakes):
    item = _lookup(knowledge_object_ids=["k1", "k2"])

    assert await resolve_gold(item, "knowledge_object", fakes.FakeStore(sql={"knowledge_objects": [("k1",), ("k2",)]})) == ({"k1", "k2"}, None)
    assert await resolve_gold(item, "knowledge_object", fakes.FakeStore(sql={"knowledge_objects": [("k1",)]})) == (None, "knowledge_object_missing")


async def test_exhaustive_predicate_change_is_stale(fakes):
    item = GoldenItem(id="e", query="q" * 12, kind="exhaustive", split="dev", predicate_sql="SELECT d.document_id FROM x", document_ids=["d1", "d2"])

    assert await resolve_gold(item, "chunk", fakes.FakeStore(sql={"FROM x": [("d2",), ("d1",)]})) == ({"d1", "d2"}, None)
    assert await resolve_gold(item, "chunk", fakes.FakeStore(sql={"FROM x": [("d1",), ("d2",), ("d3",)]})) == (None, "predicate_changed")


def test_lookup_metrics_match_hand_computation():
    metrics = lookup_metrics(["x", "g1", "y", "g2"], {"g1", "g2", "g3"})

    idcg = 1 + 1 / math.log2(3) + 1 / math.log2(4)
    assert metrics["hit@1"] == 0.0
    assert metrics["recall@5"] == pytest.approx(2 / 3)
    assert metrics["mrr"] == 0.5
    assert metrics["ndcg@10"] == pytest.approx((1 / math.log2(3) + 1 / math.log2(5)) / idcg)
    assert lookup_metrics(["g"], {"g"}) == {"hit@1": 1.0, "recall@5": 1.0, "mrr": 1.0, "ndcg@10": 1.0}


def test_knowledge_object_grain_scores_only_knowledge_object_hits():
    hits = [
        {"chunk_id": "c1", "document_id": "d1"},
        {"document_id": "d2"},
        {"knowledge_object_id": "k1", "document_id": "d1"},
        {"knowledge_object_id": "k1", "document_id": "d1"},
    ]

    ranked, metrics = score_item(_lookup(knowledge_object_ids=["k1"]), hits, "knowledge_object", {"k1"})

    assert ranked == ["k1"] and metrics["hit@1"] == 1.0


def test_exhaustive_lifts_grain_hits_to_documents_and_none_needs_empty_list():
    exhaustive = GoldenItem(id="e", query="q" * 12, kind="exhaustive", split="dev")
    hits = [{"chunk_id": "c1", "document_id": "d1"}, {"chunk_id": "c2", "document_id": "d1"}, {"document_id": "d7"}, {"chunk_id": "c3", "document_id": "d3"}]

    docs, metrics = score_item(exhaustive, hits, "chunk", {"d1", "d2"})
    assert docs == ["d1", "d3"] and metrics["recall"] == 0.5 and metrics["precision"] == 0.5

    none = GoldenItem(id="n", query="q" * 12, kind="none", split="dev")
    assert score_item(none, [{"document_id": "d7"}], "chunk", set())[1]["none_correct"] == 1.0
    assert score_item(none, hits, "chunk", set())[1]["none_correct"] == 0.0


def test_summarize_overall_does_not_average_heterogeneous_kind_metrics():
    """`overall`이 lookup ndcg@10 / exhaustive recall / none none_correct를 같은 `score`로 섞어 평균
    내면, kind 비율이 바뀔 때마다 검색 품질과 무관하게 값이 바뀐다(wiki/search-quality-review-
    2026-09-15.md 3.3). overall은 n/stale/error만 담고, 지표는 kind별로만 본다."""
    records = [
        {"status": "ok", "kind": "lookup", "metrics": {"ndcg@10": 1.0, "score": 1.0, "hit@1": 1.0, "mrr": 1.0, "recall@5": 1.0}},
        {"status": "ok", "kind": "exhaustive", "metrics": {"recall": 0.0, "precision": 0.0, "score": 0.0}},
        {"status": "ok", "kind": "none", "metrics": {"none_correct": 0.0, "score": 0.0}},
        {"status": "stale", "kind": "lookup"},
        {"status": "error", "kind": "lookup"},
    ]

    summary = summarize(records)

    assert summary["overall"] == {"n": 3, "stale": 1, "error": 1}
    assert summary["lookup"]["score"] == 1.0
    assert summary["exhaustive"]["score"] == 0.0
    assert summary["none"]["score"] == 0.0


def _golden(items, fingerprint) -> GoldenSet:
    return GoldenSet(version="chunk-test", grain="chunk", created_at="2026-09-14T00:00:00Z", corpus=fingerprint, items=items)


async def test_run_eval_retrieval_reports_metrics_stale_and_grain(fakes, monkeypatch, fingerprint):
    async def _fp(store):
        return fingerprint

    grains: list = []

    async def _retrieve(query, *, store, provider=None, use_llm_constraints=True, grain=None):
        grains.append((grain, provider, use_llm_constraints))
        return SimpleNamespace(hits=[{"chunk_id": "c1", "document_id": "d1"}], strategies=["vector"], steps=[], elapsed_ms=1.0)

    monkeypatch.setattr(run, "corpus_fingerprint", _fp)
    monkeypatch.setattr(run, "retrieve", _retrieve)
    items = [
        _lookup(evidence=[Evidence(document_id="d1", quote=QUOTE)]),
        _lookup(evidence=[Evidence(document_id="d2", quote="없는 인용문")]).model_copy(update={"id": "lookup-001", "split": "test"}),
    ]
    store = fakes.FakeStore(chunks={"d1": [("c1", QUOTE)], "d2": [("c9", "다른 내용")]})

    report = await run_eval(_golden(items, fingerprint), store)

    assert grains == [("chunk", None, False)]
    assert report["summary"]["lookup"] == {"n": 1, "hit@1": 1.0, "mrr": 1.0, "ndcg@10": 1.0, "recall@5": 1.0, "score": 1.0}
    assert report["summary"]["overall"]["stale"] == 1
    assert report["items"][1]["stale_reason"] == "quote_not_found"
    assert report["warnings"] == []
    assert set(report["provenance"]["settings"]) >= {"vector_min_score", "embedding_model"}

    only_test = await run_eval(_golden(items, fingerprint), store, split="test")
    assert [r["id"] for r in only_test["items"]] == ["lookup-001"]


async def test_e2e_ranking_follows_call_order_not_completion_order(fakes, monkeypatch, fingerprint):
    async def _fp(store):
        return fingerprint

    async def _search(args):
        await asyncio.sleep(0.05 if args["query"] == "slow" else 0.0)
        return {"hits": [{"chunk_id": f"c-{args['query']}", "document_id": "d1"}]}

    class _Tools(list):
        async def aclose(self):  # default_tools()가 돌려주는 도구 세트처럼 공유 DB 연결을 닫는 자리
            return None

    def _tools(provider=None, system=None, grain=None):
        assert grain == "chunk"
        return _Tools([ToolSpec(name="search", description="", input_schema={}, handler=_search)])

    monkeypatch.setattr(run, "corpus_fingerprint", _fp)
    monkeypatch.setattr(run, "default_tools", _tools)
    provider = fakes.ScriptedProvider(
        [("search", {"query": "slow"}), ("search", {"query": "fast"})],
        "9월 30일입니다.",
        {"verdict": "correct", "declined": False},
    )
    item = _lookup(evidence=[Evidence(document_id="d1", quote=QUOTE)])
    store = fakes.FakeStore(chunks={"d1": [("c-fast", QUOTE)]})

    report = await run_eval(_golden([item], fingerprint), store, e2e=True, provider=provider, concurrency=1)

    record = report["items"][0]
    assert record["ranked"] == ["c-slow", "c-fast"]
    assert record["metrics"]["mrr"] == 0.5
    assert record["metrics"]["llm_calls"] == 2 and record["metrics"]["tool_calls"] == 2
    assert record["metrics"]["answer_correct"] == 1.0 and record["judge"] == {"verdict": "correct", "declined": False}
