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

    def types_by_id(self) -> dict[str, list[str]]:
        return {q.id: list(q.types) for q in self.queries}

    def by_id(self, query_id: str) -> GoldenQuery:
        for q in self.queries:
            if q.id == query_id:
                return q
        raise KeyError(query_id)

    def count_by_type(self, primary_only: bool = False) -> dict[str, int]:
        counts: dict[str, int] = {}
        for q in self.queries:
            for t in [q.primary_type] if primary_only else q.types:
                counts[t] = counts.get(t, 0) + 1
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
