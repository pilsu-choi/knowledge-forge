"""Golden-set schema and loader for offline retrieval evaluation
(wiki/search-evaluation.md (C)) - the relevance judgments that Recall/
Precision/MRR/NDCG are defined against, which `search_log` alone can never
supply (a missed relevant document never shows up in traffic logs).

Design points that are easy to get wrong later:

- Query types are **multi-label** (`types`) with one `primary_type`: a real
  query is usually several things at once ("최근 세아제강 메일" is both
  relative_temporal and entity_centric), and forcing a single label makes
  the per-type breakdown lie. `primary_type` exists so a query still lands
  in exactly one bucket when a single-label view is needed.
- The scoring unit is `document_id`, not knowledge_object_id - KO-grain and
  chunk-grain embeddings (`KF_EMBEDDING_SOURCE`) only share document_id, so
  a KO-keyed gold set could not score chunk mode at all. `chunk_ids` /
  `knowledge_object_ids` are optional *extra* strictness when the mode
  under test exposes them.
- The gold file is version-pinned and immutable (`tests/eval/golden/v0.json`,
  then v1, ...): editing a version in place would silently make historical
  eval runs incomparable. `source_hash` (over the answer documents'
  `sources.content_hash`) lets `check_staleness` detect when the index has
  drifted from what the labels were written against, without touching the
  gold file.

Nothing here talks to a store: `check_staleness` takes a plain
`{document_id: content_hash}` mapping so it can be unit-tested and reused
by whichever CLI (`eval-ir`, `eval-gen`) owns the DB connection.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

# Fixed snake_case vocabulary. Adding a type is a deliberate schema change
# (the wiki's per-type scoring table must be updated alongside), so this is
# a frozenset rather than an open string field.
QUERY_TYPES: frozenset[str] = frozenset(
    {
        "factoid",
        "navigational",
        "whole_document_summary",
        "specific_document_qa",
        "temporal",
        "relative_temporal",
        "metadata_filter",
        "exact_match",
        "entity_centric",
        "multihop",
        "cross_document_comparison",
        "aggregation",
        "set_query",
        "negative",
        "latest",
        "version",
        "parent_child",
        "conversation_thread",
        "sequence",
        "long_range_context",
        "ambiguous",
        "null",
    }
)

# Types whose correct answer is "nothing relevant exists" - scored with
# `none_correct`, never with recall/precision.
NONE_TYPES: frozenset[str] = frozenset({"negative", "null"})

# dev/test 분할 라벨. **질의 자체에 박아 두고**(별도 manifest도, 실행 시
# 무작위 추출도 아니다) 파일이 곧 분할의 정본이 되게 한다 - 실행할 때마다
# 뽑는 분할은 "dev에서 튜닝하고 test에서 보고한다"는 규율을 아예 성립시키지
# 못하고(어제의 dev가 오늘의 test가 된다), 별도 manifest는 질의를 추가하면서
# manifest 갱신을 잊는 순간 조용히 어긋난다. 분할이 없는 골든셋(v0)도 그대로
# 읽혀야 하므로 기본값은 None이고, 대신 "일부만 라벨된" 상태는 GoldenSet에서
# 거부한다 - 그게 유일하게 위험한 상태다.
SPLITS: frozenset[str] = frozenset({"dev", "test"})


class GoldenExpected(BaseModel):
    """What counts as a correct retrieval for one query. The shape differs by
    type (a factoid has one document, a set query has an exhaustive list, an
    aggregation has a number, a null query has nothing), so every field is
    optional and the validator enforces the combinations that make sense."""

    document_ids: list[str] = Field(default_factory=list)
    # True: the list is the *complete* relevant set and recall is measured
    # against the whole result list (every id must be present), not @k.
    exhaustive: bool = False
    # Aggregation answer (e.g. "how many outage alerts?"); scored by exact
    # match against a predicted count the caller supplies.
    count: int | None = None
    # True: the correct answer is "no relevant document".
    none: bool = False
    chunk_ids: list[str] = Field(default_factory=list)
    knowledge_object_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_shape(self) -> GoldenExpected:
        if self.none:
            if self.document_ids or self.chunk_ids or self.knowledge_object_ids:
                raise ValueError("none=true must not list any document/chunk/knowledge_object ids")
            if self.exhaustive or self.count is not None:
                raise ValueError("none=true cannot be combined with exhaustive or count")
            return self
        if self.exhaustive and not self.document_ids:
            raise ValueError("exhaustive=true requires at least one document_id")
        if not self.document_ids and self.count is None:
            raise ValueError("expected must list document_ids, a count, or none=true")
        for name in ("document_ids", "chunk_ids", "knowledge_object_ids"):
            ids = getattr(self, name)
            if len(set(ids)) != len(ids):
                raise ValueError(f"{name} contains duplicates")
        if self.count is not None and self.count < 0:
            raise ValueError("count must be >= 0")
        return self


class GoldenQuery(BaseModel):
    id: str
    query: str
    primary_type: str
    types: list[str] = Field(min_length=1)
    expected: GoldenExpected
    # "manual" (hand-written), "synthetic" (eval-gen), "feedback" (promoted
    # from search_feedback=up traffic).
    origin: str = "manual"
    notes: str = ""
    # sha256 over the answer documents' sources.content_hash (see
    # `compute_source_hash`); None when there are no answer documents.
    source_hash: str | None = None
    # Reference "now" for relative_temporal/latest queries ("지난주", "가장
    # 최근") - without it the label is meaningless once the index grows.
    as_of: datetime | None = None
    # "dev" (knob tuning) / "test" (headline reporting) / None (unsplit set,
    # e.g. v0). See `SPLITS` and `stratified_split`.
    split: str | None = None

    @model_validator(mode="after")
    def _check_types(self) -> GoldenQuery:
        unknown = [t for t in [self.primary_type, *self.types] if t not in QUERY_TYPES]
        if unknown:
            raise ValueError(f"unknown query type(s) {sorted(set(unknown))}; allowed: {sorted(QUERY_TYPES)}")
        if self.primary_type not in self.types:
            raise ValueError(f"primary_type {self.primary_type!r} must be included in types {self.types}")
        if len(set(self.types)) != len(self.types):
            raise ValueError("types contains duplicates")
        if self.origin not in {"manual", "synthetic", "feedback"}:
            raise ValueError(f"origin must be manual|synthetic|feedback, got {self.origin!r}")
        if self.split is not None and self.split not in SPLITS:
            raise ValueError(f"split must be dev|test or absent, got {self.split!r}")
        # Type-specific shape rules. Kept here (not in GoldenExpected) because
        # the expected block alone doesn't know what kind of query it serves.
        is_none_type = self.primary_type in NONE_TYPES
        if is_none_type != self.expected.none:
            raise ValueError(
                f"{self.id}: primary_type {self.primary_type!r} requires expected.none={is_none_type}"
            )
        if self.primary_type == "aggregation" and self.expected.count is None:
            raise ValueError(f"{self.id}: aggregation queries must carry expected.count")
        if self.primary_type == "set_query" and not self.expected.exhaustive:
            raise ValueError(f"{self.id}: set_query queries must be exhaustive")
        if self.primary_type in {"relative_temporal", "latest"} and self.as_of is None:
            raise ValueError(f"{self.id}: {self.primary_type} queries must carry as_of")
        if self.primary_type == "ambiguous" and len(self.expected.document_ids) < 2:
            raise ValueError(f"{self.id}: ambiguous queries need at least two answer documents")
        return self


class IndexSnapshot(BaseModel):
    """Size of the index the labels were written against - a cheap sanity
    check that two eval runs are comparable (same golden_version *and* same
    snapshot), and a hint at how stale the file is."""

    documents: int
    chunks: int
    knowledge_objects: int
    sources_by_type: dict[str, int] = Field(default_factory=dict)


class GoldenSet(BaseModel):
    golden_version: str
    created_at: datetime
    # None = the gold set spans every project in the index (the dev DB has
    # 13 project_ids and `search.service` treats None as "all").
    project_id: str | None = None
    index_snapshot: IndexSnapshot
    queries: list[GoldenQuery] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_unique_ids(self) -> GoldenSet:
        seen: set[str] = set()
        for q in self.queries:
            if q.id in seen:
                raise ValueError(f"duplicate query id {q.id!r}")
            seen.add(q.id)
        return self

    @model_validator(mode="after")
    def _check_split_coverage(self) -> GoldenSet:
        """분할은 전부 있거나 전부 없거나 - 반쪽만 라벨된 세트를 거부한다.
        `--split test`로 돌렸는데 라벨이 안 붙은 질의가 조용히 빠지면
        헤드라인 숫자가 다른 모집단 위에서 나오고, 그건 눈에 띄지 않는다."""
        labelled = [q for q in self.queries if q.split is not None]
        if labelled and len(labelled) != len(self.queries):
            missing = [q.id for q in self.queries if q.split is None]
            raise ValueError(
                f"split is set on {len(labelled)}/{len(self.queries)} queries; "
                f"it must be set on all or none (missing: {missing[:5]}...)"
            )
        return self

    def types_by_id(self) -> dict[str, list[str]]:
        return {q.id: list(q.types) for q in self.queries}

    def by_id(self, query_id: str) -> GoldenQuery:
        for q in self.queries:
            if q.id == query_id:
                return q
        raise KeyError(query_id)

    def count_by_type(self, primary_only: bool = False, split: str | None = None) -> dict[str, int]:
        counts: dict[str, int] = {}
        for q in self.queries:
            if split is not None and q.split != split:
                continue
            for t in [q.primary_type] if primary_only else q.types:
                counts[t] = counts.get(t, 0) + 1
        return dict(sorted(counts.items()))

    def has_split(self) -> bool:
        return any(q.split is not None for q in self.queries)

    def count_by_split(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for q in self.queries:
            key = q.split or "unsplit"
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items()))


def load_golden(path: str | Path) -> GoldenSet:
    """Parse and validate a gold file. Raises `ValueError` (pydantic's
    ValidationError is one) on any invariant violation - a malformed gold set
    must never silently score."""
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    return GoldenSet.model_validate(payload)


def save_golden(golden: GoldenSet, path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    payload = golden.model_dump(mode="json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")


def compute_source_hash(content_hashes: Iterable[str]) -> str | None:
    """Order-independent digest over the answer documents' content hashes -
    sorted+deduped so re-listing the same documents in a different order
    doesn't look like drift. None when there is nothing to hash (none-type
    queries)."""
    unique = sorted(set(content_hashes))
    if not unique:
        return None
    return hashlib.sha256("\n".join(unique).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StalenessIssue:
    query_id: str
    # "missing": an answer document no longer exists in the index;
    # "changed": all documents exist but their content hashes no longer
    # reproduce source_hash (re-ingested with different content);
    # "unhashed": the query lists documents but carries no source_hash.
    reason: str
    document_ids: tuple[str, ...]


def check_staleness(golden: GoldenSet, content_hash_by_document_id: Mapping[str, str]) -> list[StalenessIssue]:
    """Pure staleness check: compare each query's answer documents against a
    `{document_id: sources.content_hash}` mapping the caller fetched. An
    empty result means the gold set still matches the index."""
    issues: list[StalenessIssue] = []
    for q in golden.queries:
        if not q.expected.document_ids:
            continue
        missing = tuple(d for d in q.expected.document_ids if d not in content_hash_by_document_id)
        if missing:
            issues.append(StalenessIssue(q.id, "missing", missing))
            continue
        if q.source_hash is None:
            issues.append(StalenessIssue(q.id, "unhashed", tuple(q.expected.document_ids)))
            continue
        current = compute_source_hash(content_hash_by_document_id[d] for d in q.expected.document_ids)
        if current != q.source_hash:
            issues.append(StalenessIssue(q.id, "changed", tuple(q.expected.document_ids)))
    return issues


# 기본 test 비율 - 절반. 두 쪽 다 "비교 가능한 반쪽"이어야 한다는 요구가
# 먼저이고, dev를 키워 튜닝 검정력을 사는 쪽(예: 6:4)은 정작 발표되는
# 쪽(test)의 신뢰구간을 넓힌다. 어느 쪽이든 반쪽 하나로는 +0.03을 가르지
# 못하므로(계산은 wiki/search-evaluation.md), 분할의 역할은 작은 효과를
# 재는 게 아니라 dev 과적합(부호 뒤집힘·큰 축소)을 잡는 것이다.
DEFAULT_TEST_FRACTION = Fraction(1, 2)


def stratified_split(
    queries: Iterable[GoldenQuery],
    test_fraction: Fraction = DEFAULT_TEST_FRACTION,
) -> dict[str, str]:
    """`primary_type`으로 층화한 결정적 dev/test 분할 - `{query_id: "dev"|"test"}`.

    난수도, 해시 기반 버킷도 쓰지 않는다. 층(=primary_type) 이름 순, 층 안에서
    질의 id 순으로 훑으면서 누적기(`acc`)에 `test_fraction`을 더해 1을 넘는
    순간만 test로 보낸다. 이렇게 하면

    - 같은 입력에 항상 같은 답이 나오고(파이썬 해시 시드·플랫폼 무관, 정수
      연산이라 부동소수 오차도 없다),
    - 층마다 test가 고르게 흩어지며,
    - 누적기가 층 경계를 넘어 이어지므로 홀수 크기 층의 나머지 한 개가 한쪽에
      몰리지 않는다(전체 test 개수가 `round(n * fraction)`에서 1 이상 벗어나지
      않는다).

    결과는 파일에 박아 넣기 위한 것이지 실행 시마다 부르라고 있는 게 아니다.
    질의를 추가하면 기존 질의의 배정도 바뀔 수 있으므로(누적기가 밀린다),
    확정된 분할은 항상 `GoldenQuery.split`에서 읽는다."""
    if not (0 < test_fraction < 1):
        raise ValueError(f"test_fraction must be strictly between 0 and 1, got {test_fraction}")
    ordered = sorted(queries, key=lambda q: (q.primary_type, q.id))
    assignment: dict[str, str] = {}
    acc = Fraction(0)
    for q in ordered:
        acc += test_fraction
        if acc >= 1:
            assignment[q.id] = "test"
            acc -= 1
        else:
            assignment[q.id] = "dev"
    return assignment
