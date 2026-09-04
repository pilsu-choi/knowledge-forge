import json
from collections import Counter
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

import pytest

from knowledge_forge.evaluation.golden import (
    NONE_TYPES,
    QUERY_TYPES,
    GoldenExpected,
    GoldenQuery,
    GoldenSet,
    IndexSnapshot,
    check_staleness,
    compute_source_hash,
    load_golden,
    save_golden,
    stratified_split,
)

V0_PATH = Path(__file__).resolve().parents[1] / "eval" / "golden" / "v0.json"
V1_PATH = Path(__file__).resolve().parents[1] / "eval" / "golden" / "v1.json"


def _snapshot() -> IndexSnapshot:
    return IndexSnapshot(documents=1, chunks=1, knowledge_objects=1, sources_by_type={"slack": 1})


def _query(**overrides) -> dict:
    base = {
        "id": "q1",
        "query": "어떤 문서?",
        "primary_type": "factoid",
        "types": ["factoid"],
        "expected": {"document_ids": ["d1"]},
    }
    base.update(overrides)
    return base


def _golden(*queries: dict) -> dict:
    return {
        "golden_version": "test",
        "created_at": "2026-09-04T00:00:00Z",
        "project_id": None,
        "index_snapshot": _snapshot().model_dump(),
        "queries": list(queries) or [_query()],
    }


def test_query_types_vocabulary_is_fixed():
    assert len(QUERY_TYPES) == 22
    assert NONE_TYPES <= QUERY_TYPES
    assert all(t == t.lower() and " " not in t for t in QUERY_TYPES)


def test_valid_golden_roundtrips_through_save_and_load(tmp_path):
    golden = GoldenSet.model_validate(_golden(_query(), _query(id="q2", primary_type="null", types=["null"], expected={"none": True})))
    path = tmp_path / "g.json"
    save_golden(golden, path)
    loaded = load_golden(path)
    assert loaded == golden
    # File must be human-diffable: UTF-8, not ASCII-escaped.
    assert "어떤 문서?" in path.read_text(encoding="utf-8")


def test_rejects_unknown_type():
    with pytest.raises(ValueError, match="unknown query type"):
        GoldenQuery.model_validate(_query(primary_type="fuzzy", types=["fuzzy"]))


def test_rejects_primary_type_not_in_types():
    with pytest.raises(ValueError, match="must be included in types"):
        GoldenQuery.model_validate(_query(primary_type="factoid", types=["navigational"]))


def test_rejects_none_with_document_ids():
    with pytest.raises(ValueError, match="none=true must not list"):
        GoldenExpected(none=True, document_ids=["d1"])


def test_rejects_none_type_without_none_flag_and_vice_versa():
    with pytest.raises(ValueError, match="requires expected.none=True"):
        GoldenQuery.model_validate(_query(primary_type="null", types=["null"], expected={"document_ids": ["d1"]}))
    with pytest.raises(ValueError, match="requires expected.none=False"):
        GoldenQuery.model_validate(_query(primary_type="factoid", types=["factoid"], expected={"none": True}))


def test_rejects_exhaustive_without_documents():
    with pytest.raises(ValueError, match="exhaustive=true requires"):
        GoldenExpected(exhaustive=True)


def test_rejects_empty_expectation():
    with pytest.raises(ValueError, match="must list document_ids, a count, or none"):
        GoldenExpected()


def test_rejects_duplicate_document_ids():
    with pytest.raises(ValueError, match="duplicates"):
        GoldenExpected(document_ids=["d1", "d1"])


def test_type_specific_rules():
    with pytest.raises(ValueError, match="aggregation queries must carry expected.count"):
        GoldenQuery.model_validate(_query(primary_type="aggregation", types=["aggregation"]))
    with pytest.raises(ValueError, match="set_query queries must be exhaustive"):
        GoldenQuery.model_validate(_query(primary_type="set_query", types=["set_query"]))
    with pytest.raises(ValueError, match="must carry as_of"):
        GoldenQuery.model_validate(_query(primary_type="latest", types=["latest"]))
    with pytest.raises(ValueError, match="at least two answer documents"):
        GoldenQuery.model_validate(_query(primary_type="ambiguous", types=["ambiguous"]))
    # count-only aggregation (no document list) is a legal shape.
    GoldenQuery.model_validate(_query(primary_type="aggregation", types=["aggregation"], expected={"count": 3}))


def test_rejects_duplicate_query_ids():
    with pytest.raises(ValueError, match="duplicate query id"):
        GoldenSet.model_validate(_golden(_query(), _query()))


def test_as_of_is_parsed_as_datetime():
    q = GoldenQuery.model_validate(
        _query(primary_type="relative_temporal", types=["relative_temporal"], as_of="2026-09-04T00:00:00Z")
    )
    assert q.as_of == datetime(2026, 9, 4, tzinfo=timezone.utc)


def test_compute_source_hash_is_order_independent_and_none_for_empty():
    assert compute_source_hash(["b", "a"]) == compute_source_hash(["a", "b", "a"])
    assert compute_source_hash(["a"]) != compute_source_hash(["b"])
    assert compute_source_hash([]) is None


def test_check_staleness_reports_missing_changed_unhashed_and_fresh():
    fresh = GoldenQuery.model_validate(_query(id="fresh", source_hash=compute_source_hash(["h1"])))
    changed = GoldenQuery.model_validate(_query(id="changed", source_hash=compute_source_hash(["old"])))
    missing = GoldenQuery.model_validate(_query(id="missing", expected={"document_ids": ["gone"]}, source_hash="x"))
    unhashed = GoldenQuery.model_validate(_query(id="unhashed"))
    none_q = GoldenQuery.model_validate(_query(id="none", primary_type="null", types=["null"], expected={"none": True}))
    golden = GoldenSet(
        golden_version="t", created_at=datetime.now(timezone.utc), index_snapshot=_snapshot(),
        queries=[fresh, changed, missing, unhashed, none_q],
    )

    issues = check_staleness(golden, {"d1": "h1"})

    assert {(i.query_id, i.reason) for i in issues} == {("changed", "changed"), ("missing", "missing"), ("unhashed", "unhashed")}
    assert next(i for i in issues if i.query_id == "missing").document_ids == ("gone",)


def test_helpers_types_by_id_and_count_by_type():
    golden = GoldenSet.model_validate(
        _golden(_query(types=["factoid", "temporal"]), _query(id="q2", primary_type="temporal", types=["temporal"]))
    )
    assert golden.types_by_id() == {"q1": ["factoid", "temporal"], "q2": ["temporal"]}
    assert golden.count_by_type() == {"factoid": 1, "temporal": 2}
    assert golden.count_by_type(primary_only=True) == {"factoid": 1, "temporal": 1}
    assert golden.by_id("q2").primary_type == "temporal"
    with pytest.raises(KeyError):
        golden.by_id("nope")


# --- the committed v0 file itself (no DB needed) ------------------------------


def test_v0_loads_and_covers_every_type():
    golden = load_golden(V0_PATH)
    assert golden.golden_version == "v0"
    assert 60 <= len(golden.queries) <= 80
    assert set(golden.count_by_type(primary_only=True)) == QUERY_TYPES
    assert all(q.origin == "manual" for q in golden.queries)


def test_v0_invariants_hold_per_query():
    golden = load_golden(V0_PATH)
    for q in golden.queries:
        # Every non-none query carries a source_hash so staleness is detectable.
        if q.expected.document_ids:
            assert q.source_hash, q.id
        else:
            assert q.expected.none or q.expected.count is not None, q.id
        if q.primary_type == "factoid":
            assert q.expected.chunk_ids, f"{q.id}: factoid must pin chunk_ids"
        if q.primary_type == "aggregation":
            assert "SQL" in q.notes, f"{q.id}: aggregation must record its counting SQL"
        if q.primary_type in {"multihop", "cross_document_comparison"}:
            assert len(q.expected.document_ids) == 2, q.id
        if q.primary_type in {"relative_temporal", "latest"}:
            assert q.as_of is not None and "as_of" in q.notes, q.id


def test_v0_ids_are_unique_and_json_is_pretty():
    raw = json.loads(V0_PATH.read_text(encoding="utf-8"))
    ids = [q["id"] for q in raw["queries"]]
    assert len(ids) == len(set(ids))
    assert raw["index_snapshot"]["documents"] > 0


def test_v0_carries_no_split():
    """v0은 분할 없는 세트로 계속 읽혀야 한다 - 파일을 고치지 않고도
    새 `split` 필드가 하위 호환된다는 확인."""
    golden = load_golden(V0_PATH)
    assert not golden.has_split()
    assert all(q.split is None for q in golden.queries)


# --- dev/test split -----------------------------------------------------------


def test_split_must_be_dev_or_test():
    with pytest.raises(ValueError):
        GoldenQuery.model_validate(_query(split="holdout"))
    assert GoldenQuery.model_validate(_query(split="dev")).split == "dev"
    assert GoldenQuery.model_validate(_query()).split is None


def test_partially_split_set_is_rejected():
    """반쪽만 라벨된 세트가 통과하면 `--split test`가 조용히 다른 모집단을
    채점한다 - 로더에서 막는다."""
    payload = _golden(_query(id="a", split="dev"), _query(id="b"))
    with pytest.raises(ValueError, match="all or none"):
        GoldenSet.model_validate(payload)


def test_fully_split_and_fully_unsplit_sets_both_load():
    split = GoldenSet.model_validate(_golden(_query(id="a", split="dev"), _query(id="b", split="test")))
    assert split.has_split() and split.count_by_split() == {"dev": 1, "test": 1}
    unsplit = GoldenSet.model_validate(_golden(_query(id="a"), _query(id="b")))
    assert not unsplit.has_split() and unsplit.count_by_split() == {"unsplit": 2}


def test_count_by_type_can_be_filtered_by_split():
    golden = GoldenSet.model_validate(
        _golden(
            _query(id="a", split="dev"),
            _query(id="b", split="test"),
            _query(id="c", primary_type="navigational", types=["navigational"], split="test"),
        )
    )
    assert golden.count_by_type(primary_only=True, split="dev") == {"factoid": 1}
    assert golden.count_by_type(primary_only=True, split="test") == {"factoid": 1, "navigational": 1}


def _split_queries(counts: dict[str, int]) -> list[GoldenQuery]:
    out = []
    for t, n in counts.items():
        for i in range(n):
            out.append(GoldenQuery.model_validate(_query(id=f"{t}-{i:03d}", primary_type=t, types=[t])))
    return out


def test_stratified_split_is_deterministic_and_balanced_per_type():
    sizes = {"factoid": 17, "navigational": 11, "exact_match": 6, "temporal": 9}
    queries = _split_queries(sizes)
    first = stratified_split(queries)
    # 입력 순서를 뒤집어도 같은 결과여야 한다(층/id 정렬로 순서를 고정하므로).
    assert stratified_split(list(reversed(queries))) == first
    assert set(first.values()) == {"dev", "test"}
    total = Counter(first.values())
    assert abs(total["dev"] - total["test"]) <= 1
    for t, n in sizes.items():
        per_type = Counter(first[q.id] for q in queries if q.primary_type == t)
        assert abs(per_type["dev"] - per_type["test"]) <= 1, (t, per_type)
        assert per_type["dev"] + per_type["test"] == n


def test_stratified_split_honours_a_non_half_fraction():
    queries = _split_queries({"factoid": 10})
    assignment = stratified_split(queries, test_fraction=Fraction(1, 5))
    assert Counter(assignment.values())["test"] == 2


def test_stratified_split_rejects_degenerate_fractions():
    queries = _split_queries({"factoid": 4})
    for bad in (Fraction(0), Fraction(1), Fraction(3, 2)):
        with pytest.raises(ValueError):
            stratified_split(queries, test_fraction=bad)


# --- the committed v1 file itself (no DB needed) ------------------------------


def test_v1_loads_split_and_covers_every_type():
    golden = load_golden(V1_PATH)
    assert golden.golden_version == "v1"
    # 목표 규모 150~200 (wiki/search-evaluation.md의 표본 크기 계산).
    assert 150 <= len(golden.queries) <= 200
    assert set(golden.count_by_type(primary_only=True)) == QUERY_TYPES
    assert golden.has_split()
    counts = golden.count_by_split()
    assert abs(counts["dev"] - counts["test"]) <= 1


def test_v1_split_is_what_stratified_split_produces():
    """파일에 박힌 분할이 기록된 알고리즘으로 재현되는지 - 분할이 손으로
    흔들렸는지 잡는다."""
    golden = load_golden(V1_PATH)
    assert {q.id: q.split for q in golden.queries} == stratified_split(golden.queries)


def test_v1_split_halves_are_comparable_per_type():
    golden = load_golden(V1_PATH)
    dev = golden.count_by_type(primary_only=True, split="dev")
    test = golden.count_by_type(primary_only=True, split="test")
    for t in QUERY_TYPES:
        assert abs(dev.get(t, 0) - test.get(t, 0)) <= 1, (t, dev.get(t, 0), test.get(t, 0))


def test_v1_invariants_hold_per_query():
    golden = load_golden(V1_PATH)
    for q in golden.queries:
        assert q.notes.strip(), f"{q.id}: notes must state the answer so a human can audit it"
        if q.expected.document_ids:
            assert q.source_hash, q.id
        else:
            assert q.expected.none or q.expected.count is not None, q.id
        if q.primary_type == "factoid":
            assert q.expected.chunk_ids, f"{q.id}: factoid must pin chunk_ids"
        if q.primary_type == "parent_child":
            assert q.expected.chunk_ids, f"{q.id}: parent_child must pin the child chunk"
        if q.primary_type == "aggregation":
            assert "SQL" in q.notes, f"{q.id}: aggregation must record its counting SQL"
            assert q.expected.count == len(q.expected.document_ids), q.id
        if q.primary_type in {"multihop", "cross_document_comparison"}:
            assert len(q.expected.document_ids) == 2, q.id
        if q.primary_type in {"relative_temporal", "latest"}:
            assert q.as_of is not None and "as_of" in q.notes, q.id
        if q.primary_type == "ambiguous":
            assert len(q.expected.document_ids) >= 2, q.id


def test_v1_carries_v0_forward_and_marks_new_queries():
    v0 = load_golden(V0_PATH)
    v1 = load_golden(V1_PATH)
    v1_ids = {q.id for q in v1.queries}
    assert {q.id for q in v0.queries} <= v1_ids, "v1 must carry every v0 query id forward"
    new_ids = v1_ids - {q.id for q in v0.queries}
    assert all(i.startswith("v1-") for i in new_ids)
    assert len(new_ids) >= 90


def test_v1_ids_are_unique_and_snapshot_is_current():
    raw = json.loads(V1_PATH.read_text(encoding="utf-8"))
    ids = [q["id"] for q in raw["queries"]]
    assert len(ids) == len(set(ids))
    assert raw["index_snapshot"]["documents"] >= 326
