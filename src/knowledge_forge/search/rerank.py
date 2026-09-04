"""Multi-stage retrieval's rerank/dedup step (wiki/search-architecture.md's
"③ Hybrid Retrieval") - merges the ranked hit lists `search/planner.py` gets
back from `file_search`/`vector_search` for one sub-query into a single
ranked, deduplicated list.

Uses Reciprocal Rank Fusion (RRF), not a Cross-Encoder/LLM reranker: `
file_search`'s per-hit signal is an integer field-match weight (title > tags
> body) and `vector_search`'s is a cosine similarity - two incomparable
scales with no shared "how relevant is this in [0, 1]" meaning, so summing
or averaging them directly would be meaningless. RRF sidesteps the
calibration problem entirely by only looking at each list's *rank order*
(already a reasonable per-source relevance ranking - that's what each
source's own `ORDER BY` already produced), which is exactly the standard
technique production hybrid search systems (Elasticsearch, Weaviate, ...)
use to fuse a keyword ranking with a vector ranking. An LLM/Cross-Encoder
reranker is a heavier, and probably eventually better, alternative - not
implemented here (see search-architecture.md) since it needs either a
dedicated model or another LLM round trip per sub-query, and RRF already
fixes the more basic problem this module exists for: `plan_search` was
previously just concatenating every source's hits with no merge at all.
"""

from __future__ import annotations

from collections.abc import Hashable
from typing import Any

DEFAULT_RRF_K = 60


def _identity_key(source: str, hit: dict[str, Any]) -> Hashable:
    """The two sources use different identifier spaces - `vector_search`
    always has `knowledge_object_id`, `file_search` only has it for
    `objects/` hits (None for `resources/`, which aren't KnowledgeObjects at
    all). A shared `knowledge_object_id` is the only safe cross-source
    dedup key; anything else (a `file_search` resource hit, or a hit shape
    this module doesn't recognize) gets a source-scoped fallback key so it
    is never accidentally merged with an unrelated hit from another source."""
    knowledge_object_id = hit.get("knowledge_object_id")
    if knowledge_object_id:
        return ("ko", knowledge_object_id)
    path = hit.get("path")
    if path:
        return ("path", source, path)
    return ("id", source, id(hit))


def merge_and_rerank(results_by_source: dict[str, list[dict[str, Any]]], k: int = DEFAULT_RRF_K) -> list[dict[str, Any]]:
    """`results_by_source` is e.g. `{"file_search": [...], "vector_search":
    [...]}`, each list already ranked best-first by its own source. Returns
    one list, ranked by summed RRF score (`1/(k+rank)` per list a hit
    appears in, so a hit both sources agree on outranks one only a single
    source found) - each returned hit carries `retrieval_sources` (every
    source that found it, replacing the per-hit `retrieval_source` the
    caller tagged it with) and `rerank_score`. A hit's original fields are
    kept; when the same identity appears in more than one source, missing
    fields are filled in from whichever source has them (first source seen
    wins on overlapping fields) rather than one hit shape overwriting the
    other's provenance-specific fields (e.g. a `vector_search` `score`
    surviving on a hit whose base shape came from `file_search`).

    A single-source input is a no-op reorder: RRF score is monotonic in
    rank within one list, so the output order matches the input order."""
    merged: dict[Hashable, dict[str, Any]] = {}
    scores: dict[Hashable, float] = {}
    sources_seen: dict[Hashable, list[str]] = {}

    for source, hits in results_by_source.items():
        for rank, hit in enumerate(hits, start=1):
            key = _identity_key(source, hit)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            if key not in merged:
                merged[key] = {field: value for field, value in hit.items() if field != "retrieval_source"}
                sources_seen[key] = []
            else:
                for field, value in hit.items():
                    if field != "retrieval_source":
                        merged[key].setdefault(field, value)
            if source not in sources_seen[key]:
                sources_seen[key].append(source)

    fused = []
    for key, hit in merged.items():
        hit = dict(hit)
        hit["retrieval_sources"] = sources_seen[key]
        hit["rerank_score"] = scores[key]
        fused.append(hit)
    fused.sort(key=lambda h: -h["rerank_score"])
    return fused
