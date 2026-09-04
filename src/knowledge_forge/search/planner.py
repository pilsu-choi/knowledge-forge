"""Query Planner - decomposes a multi-hop question into sub-queries, runs
independent sub-queries concurrently (dependency-aware: a sub-query that
depends on another only starts once its dependency's evidence is in), routes
each sub-query to the right retrieval source via `search/router.py`, and
returns the raw evidence pool for the caller (the top-level agent turn) to
synthesize an answer from - this module does not itself write a final
answer, the same division of labor `file_search`/`vector_search` already
have (retrieve, don't synthesize).

Decomposition reuses the SAME `ChatProvider` already driving the agent
session, one stateless `send()` call with an empty tool list and a
decomposition-specific system prompt - exactly `agent/compaction.py`'s
"no separate summarization model/config" pattern, applied to planning
instead of summarization. A simple/single-hop question decomposes to a
single sub-query equal to the original question, so calling this
unconditionally for "is this multi-hop" costs one extra LLM round trip but
never produces a worse plan than "just search once".

Dependency-aware execution is deliberately shallow: a dependent sub-query
doesn't get real variable substitution (there's no query language for that
here) - it gets the top few evidence snippets of what it depends on appended
as inline context, then is searched as ordinary text. This is enough for the
"Q2's result narrows Q4's search" shape from wiki/agent-search.md without
building a query-rewriting DSL for it.

Per sub-query, a route that names both sources gets its two hit lists merged
through `search/rerank.py::merge_and_rerank` (Reciprocal Rank Fusion) rather
than just concatenated - see that module for why RRF and not a raw score
merge.

Each sub-query's own text is also scanned for a temporal expression
(`search/temporal.py::parse_temporal_expression`, "어제"/"지난주"/"2025년
Q3"/...) and, if found, applied as a `since`/`until` filter on that
sub-query's `file_search`/`vector_search` calls - this is the one piece of
Query Understanding this module does itself (see that module's docstring
for why it's a heuristic parser, not an LLM call).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from knowledge_forge.chat_provider import ChatProvider, Message
from knowledge_forge.search.rerank import merge_and_rerank
from knowledge_forge.search.router import FILE_SEARCH, VECTOR_SEARCH, route_query
from knowledge_forge.search.service import file_search, vector_search
from knowledge_forge.search.temporal import TimeRange, parse_temporal_expression

if TYPE_CHECKING:
    from knowledge_forge.storage.store import PipelineStore

logger = logging.getLogger(__name__)

DEFAULT_MAX_SUBQUERIES = 5
DEFAULT_LIMIT_PER_SUBQUERY = 5
_DEPENDENCY_SNIPPET_COUNT = 3

_DECOMPOSITION_SYSTEM_PROMPT = """당신은 검색 질의 분해기다. 사용자의 질문 하나를 받아, 그 질문에
답하기 위해 지식 베이스에서 순서대로(또는 독립적으로) 검색해야 할 하위 질의 목록을 만든다.

규칙:
- 질문이 단일 검색으로 충분하면(단순 사실 조회 등) 원래 질문 그대로 하나짜리 목록을 반환할 것 -
  억지로 쪼개지 말 것.
- 여러 단계(예: "A들을 찾고, 그중 B 조건을 만족하는 것을 찾고, 그 이유를 찾는다")가 필요하면
  단계별로 하위 질의를 나눌 것 - 각 하위 질의는 그 자체로 검색 가능한 완결된 문장이어야 한다.
- 어떤 하위 질의가 앞선 하위 질의의 결과를 알아야 의미가 통하면(예: "그 프로젝트의 성공 이유"는
  "그 프로젝트"가 무엇인지 앞 단계에서 먼저 찾아야 함), depends_on에 그 앞선 하위 질의의
  1-based 인덱스(목록에서 몇 번째인지)를 적을 것 - 없으면 빈 배열.
- 서로 의존하지 않는 하위 질의는 병렬로 실행되니, 불필요한 depends_on을 넣어 직렬화하지 말 것.
- 최대 {max_subqueries}개까지만 만들 것.
- 다른 설명 없이 아래 JSON 배열 하나만 출력할 것(마크다운 코드펜스 없이):
[{{"query": "...", "depends_on": []}}, {{"query": "...", "depends_on": [1]}}]"""


@dataclass
class SubQuery:
    id: int
    query: str
    depends_on: list[int] = field(default_factory=list)


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.endswith("```"):
            text = text[: -len("```")]
    return text.strip()


def _parse_subqueries(raw_text: str, original_query: str, max_subqueries: int) -> list[SubQuery]:
    fallback = [SubQuery(id=1, query=original_query, depends_on=[])]
    try:
        items = json.loads(_strip_code_fence(raw_text))
    except (json.JSONDecodeError, TypeError):
        return fallback
    if not isinstance(items, list) or not items:
        return fallback

    subqueries: list[SubQuery] = []
    for position, item in enumerate(items[:max_subqueries], start=1):
        if not isinstance(item, dict) or not isinstance(item.get("query"), str) or not item["query"].strip():
            continue
        raw_depends_on = item.get("depends_on") or []
        if not isinstance(raw_depends_on, list):
            raw_depends_on = []
        # Only a dependency on a strictly earlier sub-query is meaningful -
        # anything else (forward reference, self-reference, garbage) is
        # dropped rather than trusted, so a malformed model response can
        # never produce a cycle or an out-of-range lookup downstream.
        depends_on = sorted({d for d in raw_depends_on if isinstance(d, int) and 1 <= d < position})
        subqueries.append(SubQuery(id=position, query=item["query"].strip(), depends_on=depends_on))

    return subqueries or fallback


async def decompose_query(query: str, provider: ChatProvider, max_subqueries: int = DEFAULT_MAX_SUBQUERIES) -> list[SubQuery]:
    system = _DECOMPOSITION_SYSTEM_PROMPT.format(max_subqueries=max_subqueries)
    try:
        turn = await provider.send([Message(role="user", content=query)], [], system)
        raw_text = turn.text or ""
    except Exception as exc:  # noqa: BLE001 - a failed decomposition must not crash the caller, just skip planning
        logger.warning("query decomposition failed, falling back to a single sub-query: %s", exc)
        raw_text = ""
    return _parse_subqueries(raw_text, query, max_subqueries)


def _topological_waves(subqueries: list[SubQuery]) -> list[list[SubQuery]]:
    """Groups `subqueries` into ordered waves where every sub-query in a wave
    has all of its dependencies satisfied by an earlier wave - waves run
    sequentially, sub-queries within a wave run concurrently. Never loops
    forever even on a malformed dependency graph: if no remaining sub-query
    is ready (a cycle - shouldn't happen given `_parse_subqueries` only
    allows backward references, but this stays correct even if that
    invariant is ever relaxed), the rest are dumped into one final wave
    rather than hanging."""
    remaining = {sq.id: sq for sq in subqueries}
    done: set[int] = set()
    waves: list[list[SubQuery]] = []
    while remaining:
        wave = [sq for sq in remaining.values() if all(d in done for d in sq.depends_on)]
        if not wave:
            wave = list(remaining.values())
        for sq in wave:
            del remaining[sq.id]
        done.update(sq.id for sq in wave)
        waves.append(wave)
    return waves


def _augment_with_dependencies(subquery: SubQuery, results_by_id: dict[int, list[dict[str, Any]]]) -> str:
    if not subquery.depends_on:
        return subquery.query
    snippets: list[str] = []
    for dep_id in subquery.depends_on:
        for hit in results_by_id.get(dep_id, [])[:_DEPENDENCY_SNIPPET_COUNT]:
            text = hit.get("summary") or hit.get("snippet") or hit.get("title")
            if text:
                snippets.append(str(text))
    if not snippets:
        return subquery.query
    return f"{subquery.query}\n\n(참고 정보: {'; '.join(snippets)})"


async def _execute_subquery(
    subquery: SubQuery,
    results_by_id: dict[int, list[dict[str, Any]]],
    available_sources: list[str],
    store: "PipelineStore | None",
    project_id: str | None,
    limit: int,
) -> tuple[list[dict[str, Any]], TimeRange | None]:
    augmented_query = _augment_with_dependencies(subquery, results_by_id)
    decision = route_query(augmented_query, available_sources)
    time_range = parse_temporal_expression(augmented_query)
    since = time_range.start if time_range else None
    until = time_range.end if time_range else None

    results_by_source: dict[str, list[dict[str, Any]]] = {}
    if FILE_SEARCH in decision.sources:
        results_by_source[FILE_SEARCH] = await file_search(
            augmented_query, project_id=project_id, limit=limit, since=since, until=until
        )
    if VECTOR_SEARCH in decision.sources and store is not None:
        results_by_source[VECTOR_SEARCH] = await vector_search(
            augmented_query, store, project_id=project_id, limit=limit, since=since, until=until
        )
    return merge_and_rerank(results_by_source), time_range


async def plan_and_search(
    query: str,
    provider: ChatProvider,
    *,
    store: "PipelineStore | None" = None,
    project_id: str | None = None,
    max_subqueries: int = DEFAULT_MAX_SUBQUERIES,
    limit_per_subquery: int = DEFAULT_LIMIT_PER_SUBQUERY,
) -> dict[str, Any]:
    """Decomposes `query`, executes the resulting sub-queries wave by wave
    (concurrently within a wave, `route_query()`-picked source(s) per
    sub-query), and returns `{"subqueries": [...], "evidence": [...]}` - the
    evidence pool a caller (the `plan_search` agent tool) hands back to the
    model to synthesize into an actual answer. Never raises for a query it
    can't decompose or a source that returns nothing - worst case this
    degrades to "search the whole query once against file_search"."""
    subqueries = await decompose_query(query, provider, max_subqueries)
    available_sources = [FILE_SEARCH] + ([VECTOR_SEARCH] if store is not None else [])

    results_by_id: dict[int, list[dict[str, Any]]] = {}
    time_range_by_id: dict[int, TimeRange | None] = {}
    for wave in _topological_waves(subqueries):
        wave_results = await asyncio.gather(
            *(
                _execute_subquery(sq, results_by_id, available_sources, store, project_id, limit_per_subquery)
                for sq in wave
            )
        )
        for sq, (hits, time_range) in zip(wave, wave_results):
            results_by_id[sq.id] = hits
            time_range_by_id[sq.id] = time_range

    return {
        "subqueries": [{"id": sq.id, "query": sq.query, "depends_on": sq.depends_on} for sq in subqueries],
        "evidence": [
            {
                "subquery_id": sq.id,
                "subquery": sq.query,
                "results": results_by_id[sq.id],
                "time_range": (
                    {
                        "label": time_range_by_id[sq.id].label,
                        "since": time_range_by_id[sq.id].start.isoformat() if time_range_by_id[sq.id].start else None,
                        "until": time_range_by_id[sq.id].end.isoformat() if time_range_by_id[sq.id].end else None,
                    }
                    if time_range_by_id[sq.id]
                    else None
                ),
            }
            for sq in subqueries
        ],
    }
