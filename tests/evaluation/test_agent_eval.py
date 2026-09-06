"""`eval-ir --mode agent` 경로 - 가짜 ChatProvider/도구로 DB도 LLM도 없이 돈다.

여기서 검증하는 것은 "에이전트가 잘 검색하나"가 아니라 **어댑터가 에이전트의 행동을
정확히 기록하나**다: 어떤 문서를 어떤 순서로 봤는지, 시간이 검색/생성으로 어떻게
갈리는지, 회차가 어떻게 접히는지.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest

from knowledge_forge.chat_provider import AssistantTurn, ChatProvider, Message, ToolCall, ToolSpec
from knowledge_forge.evaluation.agent_runner import (
    _harvest_document_ids,
    _harvest_predicted_count,
    _instrument_tools,
    _Recorder,
    _TimingProvider,
    make_agent_retrieve_fn,
)
from knowledge_forge.evaluation.golden import GoldenExpected, GoldenQuery, GoldenSet, IndexSnapshot
from knowledge_forge.evaluation.ir_runner import latency_summary, run_ir_eval
from knowledge_forge.storage.eval_store import EvalRunQuery

AS_OF = datetime(2026, 9, 1, tzinfo=timezone.utc)
STORE = object()


# --- 문서 id 수확 ------------------------------------------------------------------


def _harvest(payload: Any) -> list[str]:
    out: list[str] = []
    _harvest_document_ids(payload, out, set())
    return out


def test_ranked_document_ids_win_over_the_hits_that_repeat_them():
    """`search` payload는 같은 문서를 `document_ids`(랭킹)와 `hits` 양쪽에 싣는다.
    랭킹 쪽이 먼저 반영되고 중복은 무시돼야 한다."""
    payload = {
        "hits": [{"document_id": "c"}, {"document_id": "a"}],
        "document_ids": ["a", "b"],
    }
    # dict의 키 순서상 hits가 먼저 나오지만, 랭킹 목록을 명시적으로 먼저 본다.
    assert _harvest(payload) == ["a", "b", "c"]


def test_harvest_walks_lists_and_nested_shapes():
    """도구마다 결과 모양이 다르다 - list[dict](document_search), rows를 감싼
    dict(sql_search), 단일 dict(read_document)."""
    assert _harvest([{"document_id": "x"}, {"document_id": "y"}]) == ["x", "y"]
    assert _harvest({"rows": [{"document_id": "r1"}, {"document_id": "r2"}]}) == ["r1", "r2"]
    assert _harvest({"document_id": "solo", "title": "t"}) == ["solo"]


def test_harvest_ignores_non_string_ids_and_dedups_across_calls():
    out: list[str] = []
    seen: set[str] = set()
    _harvest_document_ids({"document_ids": ["a", 7, None, "b"]}, out, seen)
    _harvest_document_ids({"document_ids": ["b", "c"]}, out, seen)
    assert out == ["a", "b", "c"]


def test_predicted_count_takes_the_first_one_seen():
    assert _harvest_predicted_count({"predicted_count": 3, "hits": [{"predicted_count": 9}]}) == 3
    assert _harvest_predicted_count({"hits": [{"predicted_count": 9}]}) == 9
    assert _harvest_predicted_count({"hits": []}) is None


# --- grain -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grain_reaches_the_agents_tool_set(monkeypatch):
    """`eval-ir --grain`은 파이프라인 run만 좁히면 의미가 없다 - 에이전트 run도 같은
    코퍼스를 봐야 두 경로를 비교할 수 있다. 전략 선택과 달리 grain은 "무엇을 보느냐"라
    모델에게 맡길 축이 아니다."""
    captured: dict[str, Any] = {}

    def _fake_default_tools(**kwargs: Any) -> list[ToolSpec]:
        captured.update(kwargs)
        return []

    async def _fake_run_agent(query, tools, system, provider, max_iterations=8):
        return "답변", []

    monkeypatch.setattr("knowledge_forge.agent.tools.default_tools", _fake_default_tools)
    monkeypatch.setattr("knowledge_forge.agent.loop.run_agent", _fake_run_agent)

    fn = make_agent_retrieve_fn(provider=_ScriptedProvider([]))
    await fn(
        "질문",
        store=None,
        provider=_ScriptedProvider([]),
        project_id="p",
        now=None,
        limit=10,
        use_llm_constraints=False,
        strategies_override=None,
        grain="chunk",
    )

    assert captured["grain"] == "chunk"


# --- 도구 계측 ---------------------------------------------------------------------


def _tool(name: str, result: Any) -> ToolSpec:
    async def handler(args: dict[str, Any]) -> Any:
        return result

    return ToolSpec(name=name, description="", input_schema={}, handler=handler)


@pytest.mark.asyncio
async def test_only_retrieval_tools_contribute_to_the_ranking():
    """`write_memory`가 payload에 document_id를 실어도 랭킹에 들어가면 안 된다 -
    그건 검색 결과가 아니라 부수효과의 반환값이다."""
    recorder = _Recorder()
    tools = _instrument_tools(
        [_tool("search", {"document_ids": ["a"]}), _tool("write_memory", {"document_id": "z"})],
        recorder,
    )
    for spec in tools:
        await spec.handler({})

    assert recorder.document_ids == ["a"]
    assert recorder.tool_calls == 2  # 호출 횟수는 둘 다 센다
    assert [s.strategy for s in recorder.steps] == ["search", "write_memory"]
    assert [s.detail["counted_as_retrieval"] for s in recorder.steps] == [True, False]


@pytest.mark.asyncio
async def test_instrumentation_does_not_mutate_the_original_toolspecs():
    """`default_tools()`가 돌려준 객체를 제자리에서 고치면 같은 프로세스의 대화형
    세션까지 계측당한다."""
    original = _tool("search", {"document_ids": ["a"]})
    handler_before = original.handler
    _instrument_tools([original], _Recorder())
    assert original.handler is handler_before


@pytest.mark.asyncio
async def test_a_failing_tool_is_still_counted_and_reraises():
    """도구 실패도 호출이다 - 안 세면 "다섯 번 불렀는데 tool_calls가 3"이 된다."""

    async def boom(args: dict[str, Any]) -> Any:
        raise RuntimeError("tool exploded")

    spec = ToolSpec(name="search", description="", input_schema={}, handler=boom)
    recorder = _Recorder()
    (wrapped,) = _instrument_tools([spec], recorder)

    with pytest.raises(RuntimeError, match="tool exploded"):
        await wrapped.handler({})
    assert recorder.tool_calls == 1
    assert recorder.document_ids == []


# --- 생성 시간 계측 ----------------------------------------------------------------


class _ScriptedProvider(ChatProvider):
    """미리 정한 턴을 순서대로 돌려준다. 마지막 턴은 도구 호출이 없어야
    `run_agent`가 끝난다."""

    def __init__(self, turns: list[AssistantTurn]) -> None:
        self.turns = list(turns)
        self.sent: list[list[Message]] = []
        self.systems: list[str] = []

    async def send(self, messages: list[Message], tools: list[ToolSpec], system: str) -> AssistantTurn:
        self.sent.append(list(messages))
        self.systems.append(system)
        return self.turns.pop(0) if self.turns else AssistantTurn(text="done", tool_calls=[], stop_reason="end_turn")


@pytest.mark.asyncio
async def test_timing_provider_counts_calls_and_accumulates_generation_time():
    recorder = _Recorder()
    provider = _TimingProvider(_ScriptedProvider([]), recorder)
    await provider.send([], [], "sys")
    await provider.send([], [], "sys")
    assert recorder.llm_calls == 2
    assert recorder.generation_ms >= 0.0


@pytest.mark.asyncio
async def test_timing_provider_counts_a_failed_send():
    class _Boom(ChatProvider):
        async def send(self, messages, tools, system):  # type: ignore[no-untyped-def]
            raise RuntimeError("api down")

    recorder = _Recorder()
    with pytest.raises(RuntimeError):
        await _TimingProvider(_Boom(), recorder).send([], [], "sys")
    assert recorder.llm_calls == 1


# --- run_ir_eval: 반복 / 타이밍 / judge ---------------------------------------------


@dataclass
class FakeStep:
    strategy: str
    source: str
    n_hits: int
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class FakeAgentResult:
    """`AgentRetrievalResult`가 러너에 보이는 만큼만 흉내 낸다."""

    document_ids: list[str]
    scores: list[float] = field(default_factory=list)
    predicted_count: int | None = None
    strategies: list[str] = field(default_factory=lambda: ["search"])
    steps: list[FakeStep] = field(default_factory=lambda: [FakeStep("search", "agent_tool", 1)])
    elapsed_ms: float = 10.0
    answer: str = "답변"
    evidence: list[dict] = field(default_factory=list)
    retrieval_ms: float = 10.0
    generation_ms: float = 90.0
    total_ms: float = 100.0
    llm_calls: int = 2
    tool_calls: int = 1


def _golden(queries: list[GoldenQuery]) -> GoldenSet:
    return GoldenSet(
        golden_version="vtest",
        created_at=AS_OF,
        project_id=None,
        index_snapshot=IndexSnapshot(documents=1, chunks=1, knowledge_objects=1),
        queries=queries,
    )


Q_FACTOID = GoldenQuery(
    id="q-factoid",
    query="what is A?",
    primary_type="factoid",
    types=["factoid"],
    expected=GoldenExpected(document_ids=["A"]),
)
Q_NULL = GoldenQuery(
    id="q-null",
    query="nothing here",
    primary_type="null",
    types=["null"],
    expected=GoldenExpected(none=True),
)


class _VaryingRetriever:
    """회차마다 다른 결과를 낸다 - 에이전트 비결정성의 스탠드인."""

    def __init__(self, rankings: list[list[str]]) -> None:
        self.rankings = list(rankings)
        self.calls = 0

    async def __call__(self, query: str, **kwargs: Any) -> FakeAgentResult:
        ranking = self.rankings[self.calls % len(self.rankings)]
        self.calls += 1
        return FakeAgentResult(document_ids=list(ranking))


@pytest.mark.asyncio
async def test_repeats_are_folded_into_mean_and_std_on_one_row():
    """회차를 행으로 펴지 않는다 - PK가 (run_id, query_id)로 남아야 옛 run 150개와
    `--compare`가 계속 짝지어진다."""
    retriever = _VaryingRetriever([["A"], ["X", "A"], ["A"]])
    run = await run_ir_eval(
        _golden([Q_FACTOID]),
        store=STORE,  # type: ignore[arg-type]
        eval_store=None,
        retrieve_fn=retriever,  # type: ignore[arg-type]
        mode="agent",
        repeats=3,
    )
    (row,) = run.queries
    assert retriever.calls == 3
    assert row.repeats is not None and len(row.repeats) == 3
    # 1위/2위/1위 → ndcg@10은 1.0, 0.63…, 1.0. 평균은 1.0 미만이고 흔들림이 0이 아니다.
    assert 0.5 < row.metrics["ndcg@10"] < 1.0
    assert row.metrics_std is not None and row.metrics_std["ndcg@10"] > 0.0
    assert run.summary["repeats"] == 3


@pytest.mark.asyncio
async def test_a_single_repeat_leaves_the_variance_columns_empty():
    """repeats=1에서 std를 0.0으로 채우면 "흔들림을 쟀는데 0이었다"로 읽힌다.
    재지 않은 것은 비어 있어야 한다."""
    run = await run_ir_eval(
        _golden([Q_FACTOID]),
        store=STORE,  # type: ignore[arg-type]
        eval_store=None,
        retrieve_fn=_VaryingRetriever([["A"]]),  # type: ignore[arg-type]
        mode="agent",
    )
    (row,) = run.queries
    assert row.repeats is None
    assert row.metrics_std is None


@pytest.mark.asyncio
async def test_one_failing_repeat_does_not_discard_the_successful_ones():
    """3회 중 1회가 API 타임아웃이라고 성공한 2회를 버리면 에이전트 경로에서는
    그런 이유로 질의가 대량 탈락한다."""
    calls = {"n": 0}

    async def flaky(query: str, **kwargs: Any) -> FakeAgentResult:
        calls["n"] += 1
        if calls["n"] == 2:
            raise TimeoutError("provider timed out")
        return FakeAgentResult(document_ids=["A"])

    run = await run_ir_eval(
        _golden([Q_FACTOID]),
        store=STORE,  # type: ignore[arg-type]
        eval_store=None,
        retrieve_fn=flaky,  # type: ignore[arg-type]
        mode="agent",
        repeats=3,
    )
    (row,) = run.queries
    assert row.error is None
    assert row.metrics["ndcg@10"] == 1.0
    assert row.repeats is not None
    assert sum(1 for a in row.repeats if "error" in a) == 1


@pytest.mark.asyncio
async def test_every_repeat_failing_is_still_an_error_row():
    async def always_boom(query: str, **kwargs: Any) -> FakeAgentResult:
        raise RuntimeError("nope")

    run = await run_ir_eval(
        _golden([Q_FACTOID]),
        store=STORE,  # type: ignore[arg-type]
        eval_store=None,
        retrieve_fn=always_boom,  # type: ignore[arg-type]
        mode="agent",
        repeats=2,
    )
    (row,) = run.queries
    assert row.error is not None and "nope" in row.error
    assert row.metrics == {}
    assert run.summary["errors"][0]["query_id"] == "q-factoid"


@pytest.mark.asyncio
async def test_timings_land_in_their_own_columns_and_the_summary():
    run = await run_ir_eval(
        _golden([Q_FACTOID]),
        store=STORE,  # type: ignore[arg-type]
        eval_store=None,
        retrieve_fn=_VaryingRetriever([["A"]]),  # type: ignore[arg-type]
        mode="agent",
    )
    (row,) = run.queries
    assert row.retrieval_ms == 10.0
    assert row.generation_ms == 90.0
    assert row.total_ms == 100.0
    assert row.llm_calls == 2 and row.tool_calls == 1
    assert row.answer == "답변"
    latency = run.summary["latency"]
    assert latency["generation_ms"]["mean"] == 90.0
    assert latency["total_ms"]["p95"] == 100.0


def test_latency_summary_omits_metrics_nothing_measured():
    """파이프라인 run의 리포트에 `generation_ms: 0`이 찍히면 "생성이 공짜였다"로
    읽힌다. 안 잰 것은 키 자체가 없어야 한다."""
    rows = [
        EvalRunQuery(
            run_id="r", query_id=f"q{i}", query_text="", primary_type="factoid", types=[],
            retrieval_ms=float(i), total_ms=float(i * 2),
        )
        for i in range(1, 21)
    ]
    summary = latency_summary(rows)
    assert "generation_ms" not in summary
    assert summary["retrieval_ms"]["p50"] == 10.0
    # nearest-rank라 항상 실제로 관측된 값이다 - 보간된 19.05 같은 값이 나오면 안 된다.
    assert summary["retrieval_ms"]["p95"] == 19.0
    assert summary["retrieval_ms"]["max"] == 20.0


@pytest.mark.asyncio
async def test_agent_mode_without_judge_warns_about_none_type_scoring():
    run = await run_ir_eval(
        _golden([Q_NULL]),
        store=STORE,  # type: ignore[arg-type]
        eval_store=None,
        retrieve_fn=_VaryingRetriever([["A"]]),  # type: ignore[arg-type]
        mode="agent",
    )
    assert any("none_correct" in w for w in run.summary["mode_warnings"])


@pytest.mark.asyncio
async def test_pipeline_mode_carries_no_agent_warning_and_no_generation_time():
    run = await run_ir_eval(
        _golden([Q_NULL]),
        store=STORE,  # type: ignore[arg-type]
        eval_store=None,
        retrieve_fn=_VaryingRetriever([[]]),  # type: ignore[arg-type]
        mode="pipeline",
    )
    assert "mode_warnings" not in run.summary
    assert run.summary["mode"] == "pipeline"


class _Verdict:
    def __init__(self, claims: list[dict], groundedness: float = 1.0) -> None:
        self.groundedness_score = groundedness
        self.citation_precision = None
        self.claims = claims


@pytest.mark.asyncio
async def test_judge_rescores_none_queries_from_the_answer(monkeypatch):
    """에이전트는 후보를 훑고 나서 "없다"고 답하는 게 정상이라, 회수 문서 수로
    none_correct를 매기면 옳게 행동한 에이전트가 0점이 된다.

    claims가 **비어 있지 않은데도** 1.0이 나와야 한다는 게 이 테스트의 핵심이다.
    실측에서 에이전트는 "찾지 못했습니다"라고 옳게 답하면서 자기 검색 과정을
    검증 가능한 주장으로 설명했고, claims의 공백으로 추론하던 옛 판정은 정확히
    반대 결론(none_correct 0.0)을 냈다."""

    async def fake_judge(log, provider):  # type: ignore[no-untyped-def]
        return _Verdict(claims=[{"claim": "벡터 검색 0건", "verdict": "supported"}])

    async def fake_declined(query, answer, provider):  # type: ignore[no-untyped-def]
        assert query == "nothing here"
        assert "찾지 못했습니다" in answer
        return True

    monkeypatch.setattr("knowledge_forge.evaluation.judge.judge_search_log", fake_judge)
    monkeypatch.setattr("knowledge_forge.evaluation.judge.judge_answer_declined", fake_declined)

    async def retriever(query: str, **kwargs: Any) -> FakeAgentResult:
        # 후보는 실제로 봤다 - 그래서 문서 기반 판정이면 0점이 나올 상황이다.
        return FakeAgentResult(
            document_ids=["A", "B"], answer="여러 방식으로 검색했지만 관련 문서를 찾지 못했습니다."
        )

    run = await run_ir_eval(
        _golden([Q_NULL]),
        store=STORE,  # type: ignore[arg-type]
        eval_store=None,
        retrieve_fn=retriever,  # type: ignore[arg-type]
        mode="agent",
        judge_provider=_ScriptedProvider([]),
    )
    (row,) = run.queries
    assert row.metrics["none_correct"] == 1.0
    assert row.metrics["groundedness"] == 1.0


@pytest.mark.asyncio
async def test_an_agent_that_answers_a_null_query_scores_zero(monkeypatch):
    """반대 방향 - 없는 걸 있다고 답하면 0점이어야 한다. 위 테스트만 있으면
    "항상 1.0을 준다"는 구현으로도 통과한다."""

    async def fake_judge(log, provider):  # type: ignore[no-untyped-def]
        return _Verdict(claims=[{"claim": "육아휴직은 30일이다", "verdict": "unsupported"}], groundedness=0.0)

    async def fake_declined(query, answer, provider):  # type: ignore[no-untyped-def]
        return False

    monkeypatch.setattr("knowledge_forge.evaluation.judge.judge_search_log", fake_judge)
    monkeypatch.setattr("knowledge_forge.evaluation.judge.judge_answer_declined", fake_declined)

    async def retriever(query: str, **kwargs: Any) -> FakeAgentResult:
        return FakeAgentResult(document_ids=["A"], answer="육아휴직은 30일까지 쓸 수 있습니다.")

    run = await run_ir_eval(
        _golden([Q_NULL]),
        store=STORE,  # type: ignore[arg-type]
        eval_store=None,
        retrieve_fn=retriever,  # type: ignore[arg-type]
        mode="agent",
        judge_provider=_ScriptedProvider([]),
    )
    (row,) = run.queries
    assert row.metrics["none_correct"] == 0.0


@pytest.mark.asyncio
async def test_an_unparseable_declined_judgement_leaves_the_document_based_value(monkeypatch):
    """판정이 실패하면 지어내지 않는다. `score_query`가 매긴 문서 기반 값이 남고,
    summary의 경고와 함께 읽혀야 한다."""

    async def fake_judge(log, provider):  # type: ignore[no-untyped-def]
        return None

    async def fake_declined(query, answer, provider):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr("knowledge_forge.evaluation.judge.judge_search_log", fake_judge)
    monkeypatch.setattr("knowledge_forge.evaluation.judge.judge_answer_declined", fake_declined)

    async def retriever(query: str, **kwargs: Any) -> FakeAgentResult:
        return FakeAgentResult(document_ids=["A"], answer="뭔가 답했다")

    run = await run_ir_eval(
        _golden([Q_NULL]),
        store=STORE,  # type: ignore[arg-type]
        eval_store=None,
        retrieve_fn=retriever,  # type: ignore[arg-type]
        mode="agent",
        judge_provider=_ScriptedProvider([]),
    )
    (row,) = run.queries
    assert row.metrics["none_correct"] == 0.0  # 문서를 돌려줬으므로 문서 기반 판정은 0
    assert "groundedness" not in row.metrics


@pytest.mark.asyncio
async def test_judge_failure_leaves_the_metric_unscored_rather_than_zero(monkeypatch):
    async def fake_judge(log, provider):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr("knowledge_forge.evaluation.judge.judge_search_log", fake_judge)

    run = await run_ir_eval(
        _golden([Q_FACTOID]),
        store=STORE,  # type: ignore[arg-type]
        eval_store=None,
        retrieve_fn=_VaryingRetriever([["A"]]),  # type: ignore[arg-type]
        mode="agent",
        judge_provider=_ScriptedProvider([]),
    )
    (row,) = run.queries
    assert "groundedness" not in row.metrics
    assert row.metrics["ndcg@10"] == 1.0


@pytest.mark.asyncio
async def test_mode_and_repeats_change_the_config_hash():
    """pipeline run과 agent run은 애초에 다른 것을 잰다 - config_hash가 같으면
    `--compare`가 "같은 설정끼리 비교 = run-to-run 노이즈" 경고를 못 낸다."""
    kwargs: dict[str, Any] = {
        "store": STORE,
        "eval_store": None,
        "retrieve_fn": _VaryingRetriever([["A"]]),
    }
    pipeline = await run_ir_eval(_golden([Q_FACTOID]), mode="pipeline", **kwargs)  # type: ignore[arg-type]
    agent = await run_ir_eval(_golden([Q_FACTOID]), mode="agent", **kwargs)  # type: ignore[arg-type]
    thrice = await run_ir_eval(_golden([Q_FACTOID]), mode="agent", repeats=3, **kwargs)  # type: ignore[arg-type]
    assert len({pipeline.config_hash, agent.config_hash, thrice.config_hash}) == 3


@pytest.mark.asyncio
async def test_recording_survives_a_real_multi_turn_agent_loop():
    """계측을 `agent/loop.py`와 실제로 붙여 본다 - 어댑터가 하는 조립(도구 래핑 +
    provider 래핑 + run_agent)을 그대로 재현한다.

    여러 턴에 걸친 순서가 핵심이다: 1턴에서 본 문서가 2턴에서 본 문서보다 앞서야
    한다. 에이전트에게는 먼저 찾은 것이 상위 후보이고, 그 순서가 곧 랭킹이다."""
    from knowledge_forge.agent.loop import run_agent

    recorder = _Recorder()
    tools = _instrument_tools(
        [
            _tool("search", {"document_ids": ["A", "B"], "predicted_count": 2}),
            _tool("read_document", {"document_id": "C"}),
        ],
        recorder,
    )
    provider = _TimingProvider(
        _ScriptedProvider(
            [
                AssistantTurn(text=None, tool_calls=[ToolCall("1", "search", {"query": "q"})], stop_reason="tool_use"),
                AssistantTurn(
                    text=None, tool_calls=[ToolCall("2", "read_document", {"document_id": "C"})], stop_reason="tool_use"
                ),
                AssistantTurn(text="정리하면 …", tool_calls=[], stop_reason="end_turn"),
            ]
        ),
        recorder,
    )

    answer, _messages = await run_agent("질문", tools, "sys", provider, max_iterations=8)

    assert answer == "정리하면 …"
    assert recorder.document_ids == ["A", "B", "C"]  # 1턴이 2턴보다 앞선다
    assert recorder.predicted_count == 2
    assert recorder.tool_calls == 2
    assert recorder.llm_calls == 3  # 도구 턴 2 + 마무리 턴 1
    assert [s.n_hits for s in recorder.steps] == [2, 1]
    assert len(recorder.evidence) == 2
