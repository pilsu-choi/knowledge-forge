"""Generation gates: rule gate, label expansion, none acceptance, stratified sampling."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from knowledge_forge.config import settings
from knowledge_forge.eval import generate
from knowledge_forge.eval.generate import (
    _Generator,
    anchor_quote,
    generate_golden,
    pool_candidate_ids,
    rule_gate,
    split_for,
    split_key,
    stratify,
    target_mix,
)
from knowledge_forge.eval.goldenset import Evidence, GoldenItem
from knowledge_forge.exceptions import EvalError

SOURCE = "2026년 3분기 NH농협은행 PF 에이전트 프로젝트는 9월 30일에 운영 환경으로 배포하기로 결정했다."


@pytest.mark.parametrize(
    ("query", "reason"),
    [
        ("배포일은?", "too_short"),
        ("C0BT6M412N9 채널에서 결정된 배포 일정은 언제인가", "raw_id"),
        ("문서 0123456789abcdef0123456789abcdef 의 작성자는 누구인가", "raw_id"),
        ("PF 에이전트 프로젝트가 운영에 배포되었나요?", "yes_no"),
        ("PF 에이전트 운영 배포 여부를 알려줘", "yes_no"),
        ("이 프로젝트의 운영 배포 예정일은 언제인가요?", "deictic"),
        ("PF 프로젝트는 9월 30일에 운영 환경으로 배포하기로 결정했다는데 맞아", "copies_source"),
        ("NH농협은행 PF 에이전트의 운영 배포 예정일은 언제인가요?", None),
    ],
)
def test_rule_gate(query, reason):
    assert rule_gate(query, SOURCE) == reason


def test_anchor_quote_pins_a_near_verbatim_quote_to_source_text():
    assert anchor_quote(SOURCE, "9월 30일에 운영 환경으로") == "9월 30일에 운영 환경으로"
    # the model dropped the trailing punctuation and changed a word - the shared span is kept
    assert anchor_quote(SOURCE, "PF 에이전트 프로젝트는 9월 30일에 운영계로") == "PF 에이전트 프로젝트는 9월 30일에 운영"
    assert anchor_quote(SOURCE, "전혀 없는 문장") is None


def test_target_mix_scales_300_proportionally():
    assert target_mix(300) == {"lookup": 210, "exhaustive": 45, "none": 45}
    assert sum(target_mix(10).values()) == 10


def test_stratify_round_robins_source_types_and_caps_per_document():
    rows = [("a1", "da", "gmail"), ("a2", "da", "gmail"), ("a3", "da", "gmail"), ("a4", "db", "gmail"), ("s1", "ds", "slack")]

    assert [r[0] for r in stratify(rows)] == ["a1", "s1", "a2", "a4"]


def _patch_arms(monkeypatch, *, retrieve=(), vector=(), lexical=()):
    """`expand_labels`가 후보를 모으는 세 arm(전체 파이프라인/벡터 단독/어휘 단독)을 각각 통제한다 -
    비워 두면 그 arm은 아무 후보도 보태지 않는다."""

    async def _retrieve(query, *, store, use_llm_constraints=True, grain=None, **kwargs):
        return SimpleNamespace(hits=list(retrieve))

    async def _vector(query, store, limit=10, target_kind=None, **kwargs):
        return list(vector)

    async def _lexical(query, store, limit=10, **kwargs):
        return list(lexical)

    monkeypatch.setattr(generate, "retrieve", _retrieve)
    monkeypatch.setattr(generate, "vector_search", _vector)
    monkeypatch.setattr(generate, "lexical_search", _lexical)


def _chunk_item():
    return GoldenItem(id="", query="PF 에이전트 운영 배포 예정일은 언제인가", answer="9월 30일", kind="lookup", split="dev",
                      evidence=[Evidence(document_id="d1", quote="9월 30일에 운영 환경으로 배포")])


async def test_label_expansion_adds_judged_positive_with_its_quote(fakes, monkeypatch):
    _patch_arms(monkeypatch, retrieve=[{"chunk_id": "c1", "document_id": "d1"}, {"chunk_id": "c2", "document_id": "d2"}, {"document_id": "d9"}])
    store = fakes.FakeStore(
        sql={"FROM chunks WHERE chunk_id IN": [("c1", "d1", SOURCE), ("c2", "d2", "회의록: PF 배포는 9/30 운영 반영 예정")]},
        chunks={"d1": [("c1", SOURCE)], "d2": [("c2", "회의록: PF 배포는 9/30 운영 반영 예정")]},
    )
    provider = fakes.ScriptedProvider({"matches": [{"i": 0, "quote": "9/30 운영 반영 예정"}]})

    item = await _Generator(store, provider, "chunk", 0, 4).expand_labels(_chunk_item())

    assert item.evidence[1] == Evidence(document_id="d2", quote="9/30 운영 반영 예정")
    # the original gold chunk c1 is not shown to the judge again
    assert "PF 배포는" in provider.calls[0][1] and "운영 환경으로 배포하기로" not in provider.calls[0][1]


async def test_label_expansion_rejects_positive_whose_quote_is_not_in_the_candidate(fakes, monkeypatch):
    _patch_arms(monkeypatch, retrieve=[{"chunk_id": "c2", "document_id": "d2"}])
    store = fakes.FakeStore(sql={"FROM chunks WHERE chunk_id IN": [("c2", "d2", "회의록: PF 배포는 9/30")]}, chunks={"d1": [("c1", SOURCE)]})

    result = await _Generator(store, fakes.ScriptedProvider({"matches": [{"i": 0, "quote": "지어낸 구절"}]}), "chunk", 0, 4).expand_labels(_chunk_item())

    assert result == "positive_without_quote"


async def test_label_expansion_rejects_anchorless_query(fakes, monkeypatch):
    ids = [f"k{i}" for i in range(6)]
    _patch_arms(monkeypatch, retrieve=[{"knowledge_object_id": k, "document_id": "d"} for k in ids])
    store = fakes.FakeStore(sql={"FROM knowledge_objects WHERE": [(k, "d", "summary") for k in ids]})
    item = GoldenItem(id="", query="프로젝트 일정은 어떻게 되나", answer="…", kind="lookup", split="dev", knowledge_object_ids=["k0"])
    provider = fakes.ScriptedProvider({"matches": [{"i": i} for i in range(5)]})

    assert await _Generator(store, provider, "knowledge_object", 0, 4).expand_labels(item) == "anchorless"


async def test_label_expansion_keeps_item_when_retrieval_misses(fakes, monkeypatch):
    _patch_arms(monkeypatch)
    provider = fakes.ScriptedProvider()

    item = await _Generator(fakes.FakeStore(chunks={"d1": [("c1", SOURCE)]}), provider, "chunk", 0, 4).expand_labels(_chunk_item())

    assert isinstance(item, GoldenItem) and len(item.evidence) == 1 and provider.calls == []


async def test_label_expansion_counts_resolved_chunks_not_evidence_entries(fakes, monkeypatch):
    """One short quote that appears in six chunks of the same document is as anchorless as six positives."""
    _patch_arms(monkeypatch)
    store = fakes.FakeStore(chunks={"d1": [(f"c{i}", f"표 {i}행: 9월 30일에 운영 환경으로 배포") for i in range(6)]})

    assert await _Generator(store, fakes.ScriptedProvider(), "chunk", 0, 4).expand_labels(_chunk_item()) == "anchorless"


def test_pool_candidate_ids_round_robins_dedupes_and_caps():
    assert pool_candidate_ids(["a", "b", "c"], ["b", "d"], cap=3) == ["a", "b", "d"]
    assert pool_candidate_ids(["a"], [], ["a", "z"], cap=8) == ["a", "z"]
    assert pool_candidate_ids(cap=5) == []


async def test_label_expansion_pools_a_candidate_the_fused_pipeline_never_surfaces(fakes, monkeypatch):
    """A chunk that `retrieve()` misses but an independent vector-only arm finds must still reach the
    judge - otherwise it is scored wrong forever just because one pipeline missed it, which biases any
    A/B toward whichever pipeline built the labels (wiki/search-quality-review-2026-09-15.md 3.1)."""
    _patch_arms(monkeypatch, vector=[{"chunk_id": "c2", "document_id": "d2"}])
    store = fakes.FakeStore(
        sql={"FROM chunks WHERE chunk_id IN": [("c2", "d2", "회의록: PF 배포는 9/30 운영 반영 예정")]},
        chunks={"d1": [("c1", SOURCE)], "d2": [("c2", "회의록: PF 배포는 9/30 운영 반영 예정")]},
    )
    provider = fakes.ScriptedProvider({"matches": [{"i": 0, "quote": "9/30 운영 반영 예정"}]})

    item = await _Generator(store, provider, "chunk", 0, 4).expand_labels(_chunk_item())

    assert item.evidence[1] == Evidence(document_id="d2", quote="9/30 운영 반영 예정")


async def test_label_expansion_pools_a_candidate_only_the_lexical_arm_finds(fakes, monkeypatch):
    _patch_arms(monkeypatch, lexical=[{"chunk_id": "c2", "document_id": "d2", "target_kind": "chunk"}])
    store = fakes.FakeStore(
        sql={"FROM chunks WHERE chunk_id IN": [("c2", "d2", "회의록: PF 배포는 9/30 운영 반영 예정")]},
        chunks={"d1": [("c1", SOURCE)], "d2": [("c2", "회의록: PF 배포는 9/30 운영 반영 예정")]},
    )
    provider = fakes.ScriptedProvider({"matches": [{"i": 0, "quote": "9/30 운영 반영 예정"}]})

    item = await _Generator(store, provider, "chunk", 0, 4).expand_labels(_chunk_item())

    assert item.evidence[1] == Evidence(document_id="d2", quote="9/30 운영 반영 예정")


async def test_label_expansion_skips_the_lexical_arm_for_knowledge_object_grain(fakes, monkeypatch):
    """`lexical_search` only ever returns chunk hits, which can't score under KO grain - calling it would
    just be a wasted round trip."""
    calls = []

    async def _lexical(query, store, limit=10, **kwargs):
        calls.append(query)
        return []

    _patch_arms(monkeypatch)
    monkeypatch.setattr(generate, "lexical_search", _lexical)
    item = GoldenItem(id="", query="프로젝트 일정은 어떻게 되나", answer="…", kind="lookup", split="dev", knowledge_object_ids=["k0"])

    await _Generator(fakes.FakeStore(), fakes.ScriptedProvider(), "knowledge_object", 0, 4).expand_labels(item)

    assert calls == []


async def test_lookup_carries_document_id_through_for_both_grains(fakes, monkeypatch):
    """`split_key`(dev/test 분할)는 모든 lookup 항목에 `document_id`가 있어야 한다 - KO grain 항목은
    `knowledge_object_ids`만 있어서, `lookup()`이 grain과 무관하게 직접 채워야 한다."""
    _patch_arms(monkeypatch)
    query = "NH농협은행 PF 에이전트의 운영 배포 예정일은 언제인가요?"

    chunk_provider = fakes.ScriptedProvider({"query": query, "answer": "9월 30일", "quote": "9월 30일에 운영 환경으로 배포"})
    chunk_row = ("c1", "d1", "gmail", f"title: 문서\n{SOURCE}", SOURCE)
    chunk_item = await _Generator(fakes.FakeStore(chunks={"d1": [("c1", SOURCE)]}), chunk_provider, "chunk", 0, 4).lookup(chunk_row)
    assert isinstance(chunk_item, GoldenItem) and chunk_item.document_id == "d1"

    ko_provider = fakes.ScriptedProvider({"query": query, "answer": "9월 30일"})
    ko_row = ("k1", "d9", "gmail", "text", "text")
    ko_store = fakes.FakeStore(sql={"knowledge_object_id FROM knowledge_objects WHERE knowledge_object_id IN": [("k1",)]})
    ko_item = await _Generator(ko_store, ko_provider, "knowledge_object", 0, 4).lookup(ko_row)
    assert isinstance(ko_item, GoldenItem) and ko_item.document_id == "d9" and ko_item.knowledge_object_ids == ["k1"]


def test_split_key_prefers_document_id_then_the_smallest_document_ids_entry_then_the_query():
    lookup = GoldenItem(id="", query="q" * 12, kind="lookup", split="dev", document_id="d7")
    exhaustive = GoldenItem(id="", query="q" * 12, kind="exhaustive", split="dev", document_ids=["d9", "d2"])
    none_item = GoldenItem(id="", query="이건 없는 주제 질의", kind="none", split="dev")

    assert split_key(lookup) == "d7"
    assert split_key(exhaustive) == "d2"
    assert split_key(none_item) == "이건 없는 주제 질의"


def test_split_for_is_a_stable_hash_of_the_key_not_an_item_index():
    assert split_for("d1") == split_for("d1")
    assert {split_for(f"doc-{i}") for i in range(20)} == {"dev", "test"}


def test_same_document_lookups_cannot_leak_across_dev_and_test():
    """The bug this replaces split by item index parity, so a document with 2 lookups (the per-document
    cap) could land one in dev and one in test - dev-tuning would then leak into the test score."""
    a = GoldenItem(id="", query="q" * 12, kind="lookup", split="dev", document_id="shared-doc")
    b = GoldenItem(id="", query="다른 질문" + "x" * 12, kind="lookup", split="dev", document_id="shared-doc")

    assert split_for(split_key(a)) == split_for(split_key(b))


async def test_generate_golden_records_the_none_gate_threshold_in_corpus(fakes, monkeypatch, fingerprint):
    """The none gate accepts/rejects based on `settings.vector_min_score` at generation time - raising the
    threshold alone raises none_correct later without any search change. Recording it lets a later run
    tell the two apart (wiki/search-quality-review-2026-09-15.md 3.2)."""

    async def _fp(store):
        return fingerprint

    monkeypatch.setattr(generate, "corpus_fingerprint", _fp)
    monkeypatch.setattr(settings, "vector_min_score", 0.42)

    golden, _ = await generate_golden(fakes.FakeStore(), fakes.ScriptedProvider(), grain="chunk", n=0)

    assert golden.corpus["none_gate"] == {"vector_min_score": 0.42}


def _patch_vector(monkeypatch, score):
    async def _fake(query, store, limit=10, target_kind=None, **kwargs):
        assert target_kind == "chunk"
        return [{"score": score}] if score is not None else []

    monkeypatch.setattr(generate, "vector_search", _fake)


@pytest.mark.parametrize(
    ("df", "score", "expected"),
    [(0, 0.30, None), (0, None, None), (2, 0.30, "none_term_in_corpus"), (0, 0.60, "none_vector_too_similar")],
)
async def test_none_item_acceptance(fakes, monkeypatch, df, score, expected):
    _patch_vector(monkeypatch, score)
    store = fakes.FakeStore(sql={"term_index": [(0, 0), (1, df)], "SELECT title FROM documents": [("주간보고",)]})
    provider = fakes.ScriptedProvider({"query": "사내 블록체인 급여 정산 시스템 도입 일정은 언제인가", "key_terms": ["블록체인", "급여 정산"]})

    result = await _Generator(store, provider, "chunk", 0, 4).none(0, [])

    assert (result if isinstance(result, str) else None) == expected


async def test_generate_refuses_when_grain_embeddings_are_missing(fakes, monkeypatch, fingerprint):
    async def _fp(store):
        return fingerprint

    monkeypatch.setattr(generate, "corpus_fingerprint", _fp)

    with pytest.raises(EvalError, match="embed --embedding-source knowledge_object"):
        await generate_golden(fakes.FakeStore(), fakes.ScriptedProvider(), grain="knowledge_object", n=5)


@pytest.mark.parametrize(
    ("answer", "unanswerable"),
    [
        ("소유자에 대한 정보는 원문에 명시되어 있지 않습니다.", True),
        ("원문만으로는 알 수 없습니다.", True),
        ("해당 정보가 없습니다.", True),
        ("2026년 8월 26일입니다.", False),
        ("차주까지 공유될 예정입니다.", False),
    ],
)
def test_unanswerable_answer_is_detected(answer, unanswerable):
    from knowledge_forge.eval.generate import _UNANSWERABLE_RE

    assert bool(_UNANSWERABLE_RE.search(answer)) is unanswerable
