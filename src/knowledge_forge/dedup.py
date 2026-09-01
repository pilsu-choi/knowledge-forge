"""Similarity-based duplicate detection/merging for KnowledgeObjects
(wiki/roadmap.md 5단계, item 2).

The same underlying claim can be extracted more than once - e.g. two
adjacent chunks that both mention the same Decision, or (with multi-chunk
evidence, see extraction/grouping.py) two overlapping chunk groups. This
module finds same-type objects whose text is near-identical and merges
them into one canonical object, keeping a DUPLICATE_OF relation as an
audit trail of what got merged into what (mirrors how `supersedes` keeps
both the new and prior object addressable rather than silently
overwriting - see extraction/versioning.py).

Deliberately a pure function over an in-memory object/relation list, not a
store-querying job: callers decide the scope (one document's objects,
pipeline.py; or a whole run's, a future cross-source reconciliation job -
see wiki/roadmap.md 5단계 follow-ups) by choosing what they pass in.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

from knowledge_forge.ids import stable_id
from knowledge_forge.models.knowledge_object import KnowledgeObject
from knowledge_forge.models.relation import Relation, RelationType
from knowledge_forge.similarity import text_similarity

DEFAULT_DEDUP_THRESHOLD = 0.9


@dataclass
class DedupResult:
    objects: list[KnowledgeObject] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)


def _cluster_key(ko: KnowledgeObject) -> str:
    return ko.type.value


def merge_duplicates(
    objects: list[KnowledgeObject],
    relations: list[Relation],
    threshold: float = DEFAULT_DEDUP_THRESHOLD,
) -> DedupResult:
    """Merges near-duplicate, same-type objects (by `summary` similarity).

    For each cluster of near-duplicates, the object with the earliest
    `extracted_at` (ties broken by id, for determinism) is kept as
    canonical; its `evidence`/`topics` absorb the duplicates'. Every other
    relation in `relations` that referenced a merged-away id is rewritten
    to point at the canonical id instead, so the returned relation list
    never dangles.
    """
    if len(objects) < 2:
        return DedupResult(objects=list(objects), relations=list(relations))

    by_key: dict[str, list[KnowledgeObject]] = {}
    for ko in objects:
        by_key.setdefault(_cluster_key(ko), []).append(ko)

    canonical_of: dict[str, str] = {}  # duplicate id -> canonical id
    kept: list[KnowledgeObject] = []
    now = datetime.now(timezone.utc)
    new_relations: list[Relation] = []

    for group in by_key.values():
        clusters: list[list[KnowledgeObject]] = []
        for ko in group:
            placed = False
            for cluster in clusters:
                if text_similarity(ko.summary, cluster[0].summary) >= threshold:
                    cluster.append(ko)
                    placed = True
                    break
            if not placed:
                clusters.append([ko])

        for cluster in clusters:
            if len(cluster) == 1:
                kept.append(cluster[0])
                continue
            ordered = sorted(cluster, key=lambda ko: (ko.extracted_at, ko.knowledge_object_id))
            canonical, duplicates = ordered[0], ordered[1:]

            merged_evidence = list(canonical.evidence)
            seen_chunk_ids = {e.chunk_id for e in merged_evidence}
            merged_topics = list(canonical.topics)
            seen_topics = set(merged_topics)
            for dup in duplicates:
                for ev in dup.evidence:
                    if ev.chunk_id not in seen_chunk_ids:
                        merged_evidence.append(ev)
                        seen_chunk_ids.add(ev.chunk_id)
                for topic in dup.topics:
                    if topic not in seen_topics:
                        merged_topics.append(topic)
                        seen_topics.add(topic)
                canonical_of[dup.knowledge_object_id] = canonical.knowledge_object_id
                new_relations.append(
                    Relation(
                        relation_id=stable_id("dedup", dup.knowledge_object_id, canonical.knowledge_object_id),
                        type=RelationType.DUPLICATE_OF,
                        subject_id=dup.knowledge_object_id,
                        object_id=canonical.knowledge_object_id,
                        confidence=text_similarity(dup.summary, canonical.summary),
                        evidence=[e.chunk_id for e in dup.evidence],
                        created_at=now,
                    )
                )

            canonical = canonical.model_copy(update={"evidence": merged_evidence, "topics": merged_topics})
            kept.append(canonical)

    def _resolve(obj_id: str) -> str:
        # A duplicate can itself have been the canonical of another
        # duplicate's rewrite target in a degenerate chain - resolve fully.
        seen = set()
        while obj_id in canonical_of and obj_id not in seen:
            seen.add(obj_id)
            obj_id = canonical_of[obj_id]
        return obj_id

    rewritten_relations: list[Relation] = []
    for rel in relations:
        subject_id = _resolve(rel.subject_id)
        object_id = _resolve(rel.object_id)
        if subject_id == rel.subject_id and object_id == rel.object_id:
            rewritten_relations.append(rel)
        else:
            rewritten_relations.append(rel.model_copy(update={"subject_id": subject_id, "object_id": object_id}))

    return DedupResult(objects=kept, relations=rewritten_relations + new_relations)
