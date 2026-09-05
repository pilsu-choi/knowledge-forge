"""에이전트 경로를 골든셋 IR 평가에 꽂는 어댑터 (`eval-ir --mode agent`).

지금까지 평가한 경로는 `search/retrieve.py::retrieve` 하나뿐이었다 - 전략 조합을
결정론적 함수가 고르는 경로다. 그런데 사용자가 실제로 쓰는 것은 `agent/loop.py`이고,
거기서는 **모델이** 어떤 도구를 몇 번 부를지 정한다. 그 경로는 한 번도 측정된 적이
없다(`agent/tools.py::default_tools`의 "모델이 고른 조합은 측정된 적이 없고 재현되지도
않는다"가 정확히 이 공백을 가리킨다).

핵심 설계: **`ir_runner`를 고치지 않는다.** 그쪽은 이미 retriever를 주입받게 돼 있고
(`RetrieveFn` 프로토콜), 이 모듈은 그 프로토콜을 만족하는 함수를 하나 돌려줄 뿐이다.
그래서 ndcg/recall/mrr/exhaustive_recall이 그대로 돌고, 결과가 `eval_run_query`에
같은 모양으로 쌓이며, `--compare`가 파이프라인 run과 에이전트 run을 짝지어 준다.
"에이전트가 결정론적 파이프라인보다 잘 찾는가"가 같은 축의 숫자로 답해진다.

## 랭킹을 어디서 얻나

에이전트는 순위 매긴 문서 목록을 돌려주지 않는다 - 도구를 부르고 글을 쓸 뿐이다.
그래서 **도구 핸들러를 감싸서** 반환 payload에서 `document_id`를 호출 순서대로 긁어
모은다(`_harvest_document_ids`). 이 순서가 곧 랭킹이다. 근거:

- 에이전트가 먼저 부른 도구가 먼저 돌려준 문서가 그 에이전트에게는 상위 후보다.
- `search` 도구의 payload는 `document_ids`를 이미 랭킹 순으로 싣고 있어(`agent/tools.py`),
  한 번만 부르면 파이프라인 경로와 사실상 같은 순서가 나온다. 차이는 에이전트가 **여러 번**
  부르거나 다른 도구를 섞을 때 생기고, 바로 그 차이가 이 평가가 재려는 것이다.

핸들러를 감싸는 이유(= `loop.py`를 안 고치는 이유): `ToolSpec.handler`는 그냥
async callable이라 감싸면 끝이고, 루프는 자기가 감싸인 줄 모른다. 측정 코드가
프로덕션 루프에 스며들지 않는다.

## 재현성

이 경로는 LLM이라 같은 질의도 회차마다 다르다. 그래서 `--repeats`가 있고
(`ir_runner`가 회차를 돌린다), 여기서는 한 회차만 책임진다. 파이프라인 run의
`+0.0345 유의` 같은 판정을 에이전트 run에 그대로 옮겨 읽으면 안 된다 - 그쪽은
분산이 0이고 이쪽은 아니다.

## 알려진 비대칭 (해석할 때 반드시 감안할 것)

- `now`: 골든셋의 `as_of`("어제 Drive에 업로드된 …")를 에이전트에게 주려면 도구까지
  내려보내야 한다. `default_tools(now=...)`로 `search`에 전달하고, 시스템 프롬프트에도
  현재 시각을 박는다 - 둘 다 안 하면 상대 시간 질의가 경로 차이가 아니라 **기준 시각
  차이**로 틀린다.
- `predicted_count`: 도구 payload에 실려 오면 쓰고, 없으면 None이다. 에이전트가 개수를
  답변 문장으로만 말했다면 `count_match`는 채점되지 않는다(0점이 아니라 미채점).
- none 유형: 에이전트는 "후보를 훑고 나서 없다고 답하는" 게 정상이라, 회수한 문서 수로
  `none_correct`를 매기면 구조적으로 0점이 된다. 그래서 그 판정은 답변 텍스트로 옮겼다
  (`ir_runner`의 judge 연동 참고).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from knowledge_forge.chat_provider import AssistantTurn, ChatProvider, Message, ToolSpec

if TYPE_CHECKING:
    from knowledge_forge.storage.store import PipelineStore

logger = logging.getLogger(__name__)

# 이 도구들이 돌려준 것만 "검색 결과"로 친다. write_file/write_memory/write_plan 같은
# 부수효과 도구가 payload에 document_id를 실어도 랭킹에 들어가면 안 되기 때문이다.
# 목록에 없는 새 검색 도구가 생기면 여기 추가해야 한다 - 빠뜨리면 그 도구로만 찾은
# 문서가 조용히 랭킹에서 사라진다.
RETRIEVAL_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "search",
        "document_search",
        "lexical_search",
        "vector_search",
        "sql_search",
        "file_search",
        "plan_search",
        "read_document",
    }
)

# 근거(judge 입력)로 실을 도구 결과의 문자열 상한. 답변 전체와 함께 judge 프롬프트에
# 들어가므로 컨텍스트를 다 먹지 않게 자른다.
_EVIDENCE_CHARS = 2000


@dataclass
class AgentStep:
    """도구 호출 한 번. `ir_runner._steps_to_dicts`가 dataclass를 그대로 dict로
    바꾸므로 `RetrievalStep`과 같은 자리에 같은 모양으로 저장된다."""

    strategy: str  # 도구 이름 - 파이프라인 run의 "전략" 자리에 대응한다
    source: str  # "agent_tool" 고정. 파이프라인의 source(vector_search 등)와 섞이지 않게.
    n_hits: int
    detail: dict[str, Any]


@dataclass
class AgentRetrievalResult:
    """`ir_runner.RetrievalResultLike`를 만족하면서 에이전트 전용 값을 더 싣는다.
    앞쪽 다섯 필드가 프로토콜이 요구하는 전부이고, 나머지는 러너가 새 컬럼에
    옮겨 담는다."""

    document_ids: list[str]
    scores: list[float]
    predicted_count: int | None
    strategies: list[str]
    elapsed_ms: float
    steps: list[AgentStep] = field(default_factory=list)

    answer: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)
    retrieval_ms: float = 0.0
    generation_ms: float = 0.0
    total_ms: float = 0.0
    llm_calls: int = 0
    tool_calls: int = 0
    # max_iterations를 다 쓰고 강제 답변으로 끝났는가(`loop.py::_forced_final_turn`).
    # 점수가 낮을 때 "못 찾았다"와 "예산이 모자랐다"를 가르는 유일한 신호다.
    hit_iteration_cap: bool = False


def _harvest_document_ids(payload: Any, out: list[str], seen: set[str]) -> None:
    """도구 반환값을 훑어 `document_id`를 **만난 순서대로** 모은다(중복 제거).

    `document_ids`(랭킹된 리스트)를 dict의 다른 키보다 먼저 처리하는 이유: `search`
    payload는 같은 문서를 `document_ids`(순서 있음)와 `hits`(순서 있음) 양쪽에 싣는데,
    파이썬 dict는 삽입 순서를 보존하므로 대개 전자가 먼저 나오지만 **도구가 키 순서를
    바꾸면 랭킹이 조용히 뒤집힌다.** 순서에 의존하지 않도록 명시적으로 먼저 본다.

    재귀로 훑는 이유: 도구마다 결과 모양이 다르다(`search`는 dict, `document_search`는
    list[dict], `sql_search`는 rows를 감싼 dict). 도구별 파서를 두면 새 도구가 생길 때마다
    조용히 0건이 된다 - 어느 깊이에 있든 `document_id`는 문서를 가리킨다는 사실 하나에
    기대는 편이 낫다."""
    if isinstance(payload, dict):
        ranked = payload.get("document_ids")
        if isinstance(ranked, list):
            for value in ranked:
                if isinstance(value, str) and value not in seen:
                    seen.add(value)
                    out.append(value)
        single = payload.get("document_id")
        if isinstance(single, str) and single not in seen:
            seen.add(single)
            out.append(single)
        for key, value in payload.items():
            if key in ("document_ids", "document_id"):
                continue
            _harvest_document_ids(value, out, seen)
    elif isinstance(payload, list):
        for item in payload:
            _harvest_document_ids(item, out, seen)


def _harvest_predicted_count(payload: Any) -> int | None:
    """도구가 실어 보낸 `predicted_count`(집계 질의의 개수). 처음 만난 값을 쓴다 -
    에이전트가 `search`를 여러 번 불렀다면 첫 호출이 그 질의에 대한 답이고, 뒤의
    호출은 대개 보충 검색이라 개수 의미가 흐려진다."""
    if isinstance(payload, dict):
        value = payload.get("predicted_count")
        if isinstance(value, int):
            return value
        for nested in payload.values():
            found = _harvest_predicted_count(nested)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _harvest_predicted_count(item)
            if found is not None:
                return found
    return None


class _Recorder:
    """한 번의 에이전트 실행에서 도구/LLM 호출을 관찰한 결과. 호출 밖으로 살아남지
    않는다(`search/retrieve.py::_Run`과 같은 태도)."""

    def __init__(self) -> None:
        self.document_ids: list[str] = []
        self._seen: set[str] = set()
        self.predicted_count: int | None = None
        self.steps: list[AgentStep] = []
        self.evidence: list[dict[str, Any]] = []
        self.retrieval_ms = 0.0
        self.generation_ms = 0.0
        self.llm_calls = 0
        self.tool_calls = 0

    def record_tool(self, name: str, args: dict[str, Any], result: Any, elapsed_ms: float) -> None:
        self.tool_calls += 1
        before = len(self.document_ids)
        if name in RETRIEVAL_TOOL_NAMES:
            self.retrieval_ms += elapsed_ms
            _harvest_document_ids(result, self.document_ids, self._seen)
            if self.predicted_count is None:
                self.predicted_count = _harvest_predicted_count(result)
            self.evidence.append(
                {"tool": name, "args": args, "result": _truncate(result)}
            )
        self.steps.append(
            AgentStep(
                strategy=name,
                source="agent_tool",
                n_hits=len(self.document_ids) - before,
                detail={
                    "args": args,
                    "elapsed_ms": round(elapsed_ms, 2),
                    "counted_as_retrieval": name in RETRIEVAL_TOOL_NAMES,
                },
            )
        )


def _truncate(value: Any) -> str:
    text = repr(value) if not isinstance(value, str) else value
    return text[:_EVIDENCE_CHARS]


def _instrument_tools(tools: Sequence[ToolSpec], recorder: _Recorder) -> list[ToolSpec]:
    """각 도구의 핸들러를 계측 래퍼로 감싼 **새** ToolSpec 목록. 원본 ToolSpec은
    건드리지 않는다 - `default_tools()`가 돌려준 객체를 제자리에서 고치면 같은
    프로세스의 다른 호출자(대화형 세션)까지 계측당한다."""

    def wrap(spec: ToolSpec) -> ToolSpec:
        inner = spec.handler

        async def handler(args: dict[str, Any]) -> Any:
            started = time.perf_counter()
            try:
                result = await inner(args)
            except Exception:
                # 도구 실패도 호출이다 - 시간과 횟수는 세고 결과만 비운다. 세지
                # 않으면 "도구를 다섯 번 불렀는데 tool_calls가 3"이 된다.
                recorder.record_tool(spec.name, args, None, (time.perf_counter() - started) * 1000.0)
                raise
            recorder.record_tool(spec.name, args, result, (time.perf_counter() - started) * 1000.0)
            return result

        return ToolSpec(
            name=spec.name, description=spec.description, input_schema=spec.input_schema, handler=handler
        )

    return [wrap(t) for t in tools]


class _TimingProvider(ChatProvider):
    """`send()`에 스톱워치만 두른 데코레이터. 생성(LLM) 시간과 검색(도구) 시간을
    가르는 유일한 방법이다 - 벽시계 전체에서 도구 시간을 빼는 방식은 루프의
    직렬/병렬 구조가 바뀌면 조용히 틀린다(`loop.py`는 도구를 `gather`로 **동시**
    실행하므로 도구 시간의 합은 이미 벽시계보다 클 수 있다)."""

    def __init__(self, inner: ChatProvider, recorder: _Recorder) -> None:
        self._inner = inner
        self._recorder = recorder

    async def send(self, messages: list[Message], tools: list[ToolSpec], system: str) -> AssistantTurn:
        started = time.perf_counter()
        try:
            return await self._inner.send(messages, tools, system)
        finally:
            self._recorder.generation_ms += (time.perf_counter() - started) * 1000.0
            self._recorder.llm_calls += 1


def make_agent_retrieve_fn(
    *,
    provider: ChatProvider,
    max_iterations: int = 8,
    include_delegate: bool = False,
) -> Callable[..., Awaitable[AgentRetrievalResult]]:
    """`ir_runner.RetrieveFn`으로 쓸 수 있는 함수를 만든다.

    `include_delegate`가 기본 False인 이유: `delegate_task`는 하위 에이전트를 또
    돌려 LLM 호출과 시간을 곱절로 늘리는데, 그 하위 실행의 도구 호출은 이 계측이
    닿지 않는 별도 루프 안에서 일어난다. 켜면 `llm_calls`/`generation_ms`가 실제보다
    작게 기록되고 문서 회수도 누락된다 - 측정 대상으로는 부적절하다."""
    from knowledge_forge.agent.loop import run_agent
    from knowledge_forge.agent.prompts import build_system_prompt
    from knowledge_forge.agent.tools import default_tools

    async def retrieve_via_agent(
        query: str,
        *,
        store: PipelineStore,  # noqa: ARG001 - 도구가 자기 PipelineStore를 연다(프로토콜 호환용)
        provider: ChatProvider | None,  # noqa: ARG001 - 생성 시점에 받은 provider를 쓴다
        project_id: str | None,
        now: datetime | None,
        limit: int,  # noqa: ARG001 - 몇 건을 볼지는 에이전트가 정한다. 그게 측정 대상이다.
        use_llm_constraints: bool,  # noqa: ARG001 - 에이전트 경로에는 대응 개념이 없다
        strategies_override: list[str] | None,  # noqa: ARG001 - 전략 선택이 곧 에이전트의 일이다
    ) -> AgentRetrievalResult:
        recorder = _Recorder()
        timed = _TimingProvider(provider or _fail_no_provider(), recorder)
        system = build_system_prompt(project_id=project_id)
        if now is not None:
            # 골든셋의 as_of. 도구(`search` → `retrieve(now=...)`)와 프롬프트 양쪽에
            # 넣는다 - 도구만 넣으면 모델이 "어제"를 자기 학습 시점 기준으로 문장에
            # 쓰고, 프롬프트만 넣으면 실제 필터가 오늘 기준으로 돈다.
            system += f"\n\n현재 시각은 {now.isoformat()}이다. 상대 시간 표현은 이 시각을 기준으로 해석할 것."
        tools = _instrument_tools(
            default_tools(project_id=project_id, provider=timed, system=system, include_delegate=include_delegate, now=now),
            recorder,
        )

        started = time.perf_counter()
        answer, messages = await run_agent(query, tools, system, timed, max_iterations=max_iterations)
        total_ms = (time.perf_counter() - started) * 1000.0

        return AgentRetrievalResult(
            document_ids=list(recorder.document_ids),
            # 점수는 없다. 에이전트는 순위를 매기지 않고 순서만 만든다 - 없는 점수를
            # 1/rank 같은 걸로 지어내면 `none_correct`의 threshold 판정이 그 가짜
            # 숫자를 진짜처럼 쓴다(`ir_metrics.score_query`의 scores 인자).
            scores=[],
            predicted_count=recorder.predicted_count,
            strategies=[s.strategy for s in recorder.steps],
            elapsed_ms=recorder.retrieval_ms,
            steps=recorder.steps,
            answer=answer,
            evidence=recorder.evidence,
            retrieval_ms=recorder.retrieval_ms,
            generation_ms=recorder.generation_ms,
            total_ms=total_ms,
            llm_calls=recorder.llm_calls,
            tool_calls=recorder.tool_calls,
            hit_iteration_cap=_hit_cap(messages, max_iterations),
        )

    return retrieve_via_agent


def _fail_no_provider() -> ChatProvider:
    raise ValueError("--mode agent에는 ChatProvider가 필요합니다 (--provider/--model 또는 KF_AGENT_PROVIDER).")


def _hit_cap(messages: Sequence[Message], max_iterations: int) -> bool:
    """어시스턴트 턴 수가 예산과 같으면 강제 답변으로 끝났다고 본다. `run_agent`가
    그 사실을 반환값으로 알려주지 않아 메시지에서 되읽는다 - 루프의 시그니처를
    측정 편의로 바꾸지 않으려는 선택이고, 틀려도 진단 힌트 하나가 부정확할 뿐
    점수에는 영향이 없다."""
    return sum(1 for m in messages if m.role == "assistant") >= max_iterations
