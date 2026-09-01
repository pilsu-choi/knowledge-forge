"""Auto-fills `KnowledgeObject.supersedes` on re-extraction (wiki/roadmap.md
5단계, item 4).

`document_id` is `stable_id("document", uri, content_hash)` (see
parsers/*.py), so it - and every chunk/object id derived from it - changes
whenever a source's content changes. A re-extraction therefore never
reuses a prior run's ids; there is nothing to naturally overwrite. What
*is* stable across content revisions is `source_id` (derived from uri
alone, see pipeline._make_source), so matching "is this new object a new
version of an old one" means: same `source_id`, same `type`, and similar
enough text - the same similarity primitive dedup.py uses, applied across
runs instead of within one.

Matching is greedy and one-to-one: each prior object is claimed by at most
one new object (its best-similarity match), so two unrelated new objects
can't both claim the same prior id.
"""

from datetime import datetime, timezone

from knowledge_forge.ids import stable_id
from knowledge_forge.models.knowledge_object import KnowledgeObject
from knowledge_forge.models.relation import Relation, RelationType
from knowledge_forge.similarity import text_similarity

DEFAULT_SUPERSEDES_THRESHOLD = 0.8


def resolve_supersedes(
    new_objects: list[KnowledgeObject],
    prior_objects: list[KnowledgeObject],
    threshold: float = DEFAULT_SUPERSEDES_THRESHOLD,
) -> list[Relation]:
    """Mutates `new_objects` in place (setting `supersedes`/`version` on any
    object matched to a prior version) and returns the new SUPERSEDES
    relations - mirrors how pipeline.py already mutates outcome objects
    in-place before storing/yielding them, so callers don't need to
    reconstruct the outcome list from a return value.
    """
    if not new_objects or not prior_objects:
        return []

    prior_by_type: dict[str, list[KnowledgeObject]] = {}
    for prior in prior_objects:
        prior_by_type.setdefault(prior.type.value, []).append(prior)

    claimed: set[str] = set()
    now = datetime.now(timezone.utc)
    relations: list[Relation] = []

    for new_ko in sorted(new_objects, key=lambda ko: ko.knowledge_object_id):
        candidates = prior_by_type.get(new_ko.type.value, [])
        best_prior: KnowledgeObject | None = None
        best_score = 0.0
        for prior in candidates:
            if prior.knowledge_object_id in claimed:
                continue
            score = text_similarity(new_ko.summary, prior.summary)
            if score >= threshold and score > best_score:
                best_prior, best_score = prior, score
        if best_prior is None:
            continue

        claimed.add(best_prior.knowledge_object_id)
        new_ko.supersedes = best_prior.knowledge_object_id
        new_ko.version = best_prior.version + 1
        relations.append(
            Relation(
                relation_id=stable_id("supersedes", new_ko.knowledge_object_id, best_prior.knowledge_object_id),
                type=RelationType.SUPERSEDES,
                subject_id=new_ko.knowledge_object_id,
                object_id=best_prior.knowledge_object_id,
                confidence=best_score,
                evidence=[e.chunk_id for e in new_ko.evidence],
                created_at=now,
            )
        )

    return relations
