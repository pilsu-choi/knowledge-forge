"""골든셋 실행 - 기본은 retrieval(LLM 0회), `e2e=True`면 에이전트 답변까지.

채점 리스트는 골든셋 grain의 id를 가진 히트만(`goldenset.ranked_ids`). 정답 라벨은 실행 시점에 해석한다
(`resolve_gold`): 인용문을 담은 청크가 사라졌거나, KO id가 없어졌거나, 술어 결과가 달라진 항목은 stale로
빼고 보고한다. 결과는 항목별 랭킹·지표·검색 steps와 provenance(git, 랭킹 설정, 모델, 코퍼스 지문)를 담은 dict.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from knowledge_forge.agent.loop import run_agent
from knowledge_forge.agent.memory import read_memory
from knowledge_forge.agent.prompts import build_system_prompt
from knowledge_forge.agent.providers.base import ToolSpec
from knowledge_forge.agent.tools import default_tools
from knowledge_forge.chat_provider import AssistantTurn, ChatProvider, Message
from knowledge_forge.config import settings
from knowledge_forge.eval import gather_bounded
from knowledge_forge.eval.goldenset import ALL_ROWS, GoldenItem, GoldenSet, corpus_fingerprint, quote_in, ranked_ids
from knowledge_forge.search.constraints import _extract_json_object
from knowledge_forge.search.retrieve import _sql_literal, retrieve
from knowledge_forge.storage import PipelineStore

RANKING_SETTINGS = (
    "vector_min_score",
    "term_coverage_ratio",
    "term_coverage_min",
    "term_rare_df_max",
    "latest_relevance_pool",
    "embedding_model",
    "embedding_source",
    "embedding_provider",
)

_JUDGE_SYSTEM = """사내 검색 에이전트의 답변을 채점한다. 질문(query), 정답(gold_answer), 에이전트 답변(answer)을 비교한다.
- verdict: 답변이 정답의 핵심을 맞게 담으면 "correct", 일부만 맞거나 불완전하면 "partial", 틀리거나 핵심이 없으면 "wrong".
- declined: 답변이 "찾지 못했다/관련 문서가 없다/알 수 없다"처럼 답을 보류했으면 true.
- gold_answer가 비어 있으면(코퍼스에 없는 주제) verdict는 "wrong"으로 두고 declined만 정확히 표시한다.
다른 설명 없이 JSON 객체 하나만 출력: {"verdict": "correct|partial|wrong", "declined": true|false}"""


# --- 라벨 해석 / 지표 ----------------------------------------------------------------


async def resolve_gold(item: GoldenItem, grain: str, store: PipelineStore) -> tuple[set[str] | None, str | None]:
    """(정답 id 집합, stale 사유). exhaustive는 문서 id, lookup은 grain id, none은 빈 집합."""
    if item.kind == "none":
        return set(), None
    if item.kind == "exhaustive":
        rows, _ = await store.execute_readonly_query(item.predicate_sql or "", ALL_ROWS)
        current = {row[0] for row in rows}
        return (current, None) if current == set(item.document_ids) else (None, "predicate_changed")
    if grain == "knowledge_object":
        ids = set(item.knowledge_object_ids)
        rows, _ = await store.execute_readonly_query(
            f"SELECT knowledge_object_id FROM knowledge_objects WHERE knowledge_object_id IN ({', '.join(map(_sql_literal, sorted(ids)))})",
            ALL_ROWS,
        )
        return (ids, None) if ids and {row[0] for row in rows} == ids else (None, "knowledge_object_missing")
    gold: set[str] = set()
    for evidence in item.evidence:
        chunks = await store.list_chunks_by_document(evidence.document_id)
        matched = {chunk.chunk_id for chunk in chunks if quote_in(chunk.text, evidence.quote)}
        if not matched:
            return None, "quote_not_found"
        gold |= matched
    return (gold, None) if gold else (None, "no_labels")


def lookup_metrics(ranked: list[str], gold: set[str]) -> dict[str, float]:
    """binary relevance: hit@1, recall@5, mrr, ndcg@10."""
    ranks = [rank for rank, value in enumerate(ranked, start=1) if value in gold]
    dcg = sum(1 / math.log2(rank + 1) for rank in ranks if rank <= 10)
    idcg = sum(1 / math.log2(rank + 1) for rank in range(1, min(len(gold), 10) + 1))
    return {
        "hit@1": float(bool(ranks) and ranks[0] == 1),
        "recall@5": sum(1 for rank in ranks if rank <= 5) / len(gold),
        "mrr": 1 / ranks[0] if ranks else 0.0,
        "ndcg@10": dcg / idcg,
    }


def score_item(item: GoldenItem, hits: list[dict[str, Any]], grain: str, gold: set[str]) -> tuple[list[str], dict[str, float]]:
    """(채점 리스트, 지표). `score`는 kind별 대표 지표(lookup ndcg@10 / exhaustive recall / none none_correct)."""
    if item.kind == "exhaustive":
        documents = ranked_ids(hits, grain, lift_to_documents=True)
        found = len(gold.intersection(documents))
        recall = found / len(gold) if gold else 0.0
        return documents, {"recall": recall, "precision": found / len(documents) if documents else 0.0, "score": recall}
    ranked = ranked_ids(hits, grain)
    if item.kind == "none":
        correct = float(not ranked)
        return ranked, {"none_correct": correct, "score": correct}
    metrics = lookup_metrics(ranked, gold)
    return ranked, {**metrics, "score": metrics["ndcg@10"]}


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in records if r["status"] == "ok"]
    summary: dict[str, Any] = {}
    for kind in ("lookup", "exhaustive", "none"):
        group = [r["metrics"] for r in ok if r["kind"] == kind]
        if group:
            keys = sorted({key for metrics in group for key in metrics})
            summary[kind] = {"n": len(group), **{k: mean(m[k] for m in group if k in m) for k in keys}}
    shared = set.intersection(*(set(r["metrics"]) for r in ok)) if ok else set()
    summary["overall"] = {
        "n": len(ok),
        "stale": sum(r["status"] == "stale" for r in records),
        "error": sum(r["status"] == "error" for r in records),
        **{k: mean(r["metrics"][k] for r in ok) for k in sorted(shared)},
    }
    return summary


# --- e2e ---------------------------------------------------------------------------


class _CountingProvider(ChatProvider):
    def __init__(self, inner: ChatProvider) -> None:
        self.inner, self.calls = inner, 0

    async def send(self, messages: list[Message], tools: list[ToolSpec], system: str) -> AssistantTurn:
        self.calls += 1
        return await self.inner.send(messages, tools, system)


async def judge_answer(provider: ChatProvider, item: GoldenItem, answer: str) -> dict[str, Any] | None:
    turn = await provider.send(
        [Message(role="user", content=json.dumps({"query": item.query, "gold_answer": item.answer, "answer": answer}, ensure_ascii=False))],
        [],
        _JUDGE_SYSTEM,
    )
    parsed = _extract_json_object(turn.text or "")
    if not parsed or parsed.get("verdict") not in ("correct", "partial", "wrong") or not isinstance(parsed.get("declined"), bool):
        return None
    return {"verdict": parsed["verdict"], "declined": parsed["declined"]}


async def run_agent_item(item: GoldenItem, grain: str, provider: ChatProvider) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """`agent/runtime.py::run_query`와 같은 조립으로 한 턴을 돌린다. search 도구 결과는 **호출 순서**
    (디스패치 시점에 자리를 잡는다 - 한 턴의 도구 호출은 동시에 돌아 완료 순서가 뒤바뀔 수 있다)로 이어 붙인다."""
    counting = _CountingProvider(provider)
    system = build_system_prompt(memory=read_memory(None))
    searches: list[list[dict[str, Any]]] = []
    tool_calls = 0

    def _wrap(spec: ToolSpec) -> ToolSpec:
        async def _handler(args: dict[str, Any]) -> Any:
            nonlocal tool_calls
            tool_calls += 1
            if spec.name != "search":
                return await spec.handler(args)
            slot = len(searches)
            searches.append([])
            result = await spec.handler(args)
            searches[slot] = list(result.get("hits", []))
            return result

        return replace(spec, handler=_handler)

    tools = [_wrap(spec) for spec in default_tools(provider=counting, system=system, grain=grain)]
    started = time.perf_counter()
    answer, _ = await run_agent(item.query, tools, system, counting, max_iterations=settings.agent_max_iterations)
    latency_ms = (time.perf_counter() - started) * 1000.0
    judged = await judge_answer(provider, item, answer)
    metrics: dict[str, Any] = {"latency_ms": latency_ms, "llm_calls": counting.calls, "tool_calls": tool_calls}
    if judged:
        metrics["declined"] = float(judged["declined"])
        metrics["answer_correct"] = float(judged["declined"] if item.kind == "none" else judged["verdict"] == "correct")
    detail = {"answer": answer, "judge": judged, "search_calls": len(searches)}
    return [hit for hits in searches for hit in hits], {"metrics": metrics, **detail}


# --- 실행 ---------------------------------------------------------------------------


def git_revision() -> str:
    """HEAD 커밋, 작업 트리가 더러우면 `-dirty.<sha256(git diff HEAD + 추적 안 된 파일)>` - 커밋만 적으면
    커밋 안 한 변경으로 잰 결과가 그 커밋의 결과로 기록된다(이전 평가의 provenance 결함)."""
    try:
        root = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=Path(__file__).parent, capture_output=True, check=True, text=True).stdout.strip())

        def _git(*args: str) -> bytes:
            return subprocess.run(["git", *args], cwd=root, capture_output=True, check=True).stdout

        commit = _git("rev-parse", "HEAD").decode().strip()
        if not _git("status", "--porcelain").strip():
            return commit
        digest = hashlib.sha256(_git("diff", "HEAD"))
        for path in sorted(p for p in _git("ls-files", "--others", "--exclude-standard", "-z").split(b"\0") if p):
            digest.update(path + b"\0" + (root / path.decode()).read_bytes())
        return f"{commit}-dirty.{digest.hexdigest()[:16]}"
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


async def run_eval(
    golden: GoldenSet,
    store: PipelineStore,
    *,
    split: str | None = None,
    e2e: bool = False,
    provider: ChatProvider | None = None,
    llm_constraints: bool = False,
    concurrency: int = 4,
    models: dict[str, str] | None = None,
) -> dict[str, Any]:
    """골든셋 하나를 실행해 결과 dict를 돌려준다. `provider`는 e2e 에이전트·judge, 또는 `llm_constraints`의
    제약 보강(이때 retrieve의 multihop planner도 켜진다)에 쓴다 - 둘 다 아니면 LLM 호출 0회."""
    if (e2e or llm_constraints) and provider is None:
        raise ValueError("e2e/llm_constraints에는 provider가 필요하다")
    grain = golden.grain
    items = [item for item in golden.items if split is None or item.split == split]
    corpus_before = await corpus_fingerprint(store)

    async def _one(item: GoldenItem) -> dict[str, Any]:
        record: dict[str, Any] = {"id": item.id, "kind": item.kind, "split": item.split, "query": item.query}
        gold, stale = await resolve_gold(item, grain, store)
        if gold is None:
            return {**record, "status": "stale", "stale_reason": stale}
        extra: dict[str, Any] = {}
        try:
            if e2e:
                assert provider is not None
                hits, extra = await run_agent_item(item, grain, provider)
            else:
                result = await retrieve(
                    item.query, store=store, provider=provider if llm_constraints else None, use_llm_constraints=llm_constraints, grain=grain
                )
                hits = result.hits
                extra = {"strategies": result.strategies, "steps": [asdict(s) for s in result.steps], "elapsed_ms": result.elapsed_ms}
        except Exception as exc:  # noqa: BLE001 - 한 항목 실패가 수백 항목 실행을 버리게 하면 안 된다
            return {**record, "status": "error", "error": f"{type(exc).__name__}: {exc}"}
        ranked, metrics = score_item(item, hits, grain, gold)
        metrics.update(extra.pop("metrics", {}))
        return {**record, **extra, "status": "ok", "gold": sorted(gold), "ranked": ranked, "metrics": metrics}

    records = await gather_bounded((_one(item) for item in items), concurrency)
    corpus_after = await corpus_fingerprint(store)
    warnings = []
    if corpus_before != corpus_after:
        warnings.append("실행 중 코퍼스 지문이 바뀌었다 - 결과를 믿지 말 것")
    if golden.corpus.get("documents") != corpus_before.get("documents"):
        warnings.append("골든셋 생성 시점과 문서 집합 지문이 다르다")
    if corpus_before["embeddings"].get(grain, 0) == 0:
        warnings.append(f"{grain} 임베딩이 0건 - 벡터 arm이 비어 있다")
    mode = "e2e" if e2e else "retrieval"
    created_at = datetime.now(timezone.utc)
    return {
        "run_id": f"{created_at:%Y%m%dT%H%M%S}-{golden.version}-{mode}" + (f"-{split}" if split else ""),
        "created_at": created_at.isoformat(),
        "mode": mode,
        "golden": {"version": golden.version, "grain": grain, "split": split, "n_items": len(items)},
        "provenance": {
            "git": git_revision(),
            "settings": {name: getattr(settings, name) for name in RANKING_SETTINGS},
            "models": {**(models or {}), "llm_constraints": llm_constraints},
        },
        "corpus_golden": golden.corpus,
        "corpus_before": corpus_before,
        "corpus_after": corpus_after,
        "warnings": warnings,
        "summary": summarize(records),
        "items": records,
    }
