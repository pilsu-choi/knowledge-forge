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


def _identity_key(source: str, hit: dict[str, Any], by_document: bool = False) -> Hashable:
    """The two sources use different identifier spaces - `vector_search`
    always has `knowledge_object_id` for KO-grain hits (and `chunk_id` for
    chunk-grain ones, `KF_EMBEDDING_SOURCE=chunk`), `file_search` only has
    `knowledge_object_id` for `objects/` hits (None for `resources/`, which
    aren't KnowledgeObjects at all). A shared `knowledge_object_id` is the
    only safe cross-source dedup key; a `chunk_id` is only ever produced by
    `vector_search` so it dedups within that source; anything else (a
    `file_search` resource hit, or a hit shape this module doesn't
    recognize) gets a source-scoped fallback key so it is never
    accidentally merged with an unrelated hit from another source.

    `by_document`면 `document_id`가 있는 히트는 그것으로 묶는다(청크/KO 단위 히트도 문서 하나로)."""
    if by_document and hit.get("document_id"):
        return ("doc", hit["document_id"])
    knowledge_object_id = hit.get("knowledge_object_id")
    if knowledge_object_id:
        return ("ko", knowledge_object_id)
    chunk_id = hit.get("chunk_id")
    if chunk_id:
        return ("chunk", chunk_id)
    path = hit.get("path")
    if path:
        return ("path", source, path)
    return ("id", source, id(hit))


def merge_and_rerank(
    results_by_source: dict[str, list[dict[str, Any]]], k: int = DEFAULT_RRF_K, *, by_document: bool = False
) -> list[dict[str, Any]]:
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
            key = _identity_key(source, hit, by_document)
            if key not in merged:
                merged[key] = {field: value for field, value in hit.items() if field != "retrieval_source"}
                sources_seen[key] = []
            else:
                for field, value in hit.items():
                    if field != "retrieval_source":
                        merged[key].setdefault(field, value)
            # 한 리스트는 한 히트에 한 표 - 같은 히트가 리스트 안에 두 번 있으면 첫(최고) 순위만 센다.
            if source not in sources_seen[key]:
                sources_seen[key].append(source)
                scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)

    fused = []
    for key, hit in merged.items():
        hit = dict(hit)
        hit["retrieval_sources"] = sources_seen[key]
        hit["rerank_score"] = scores[key]
        fused.append(hit)
    fused.sort(key=lambda h: -h["rerank_score"])
    return fused


def source_arm(source: str) -> str:
    """소스 이름의 계열 - `lexical_search#<term>`, `document_search#semantic`은 `#` 앞이 계열이다."""
    return source.split("#", 1)[0]


def merge_by_arm(results_by_source: dict[str, list[dict[str, Any]]], k: int = DEFAULT_RRF_K) -> list[dict[str, Any]]:
    """소스 계열(arm)당 1표로 문서 id 기준 RRF를 한다. 계열 안의 리스트들(term별 fallback 등)은 먼저
    문서 id 기준 RRF로 한 순위표가 되고(여러 term에 걸린 문서가 위로 온다), 그 순위표가 바깥 RRF의
    한 표가 된다. 그래서 어휘 arm이 리스트를 몇 개 내든 벡터와 같은 한 표다. 반환된 히트의
    `retrieval_sources`는 계열 이름이다."""
    arms: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for source, hits in results_by_source.items():
        arms.setdefault(source_arm(source), {})[source] = hits
    ranked = {arm: merge_and_rerank(lists, k, by_document=True) for arm, lists in arms.items()}
    return merge_and_rerank(ranked, k, by_document=True)
