"""In-session task plan the agent can maintain via a `write_plan` tool -
mirrors Claude Code's own TodoWrite: each call replaces the *entire* step
list (not an incremental edit), so `PlanState` is always exactly what the
model last declared and `loop.py` never has to reconcile diffs.

`ToolSpec.handler` (providers/base.py) carries no session context, so
`make_plan_tool()` closes over a `PlanState` instance the same way any
stateful tool would. `AgentSession` (agent/runtime.py) creates one
`PlanState` per session - not per `run_agent()` call - so the plan survives
across `--interactive` turns exactly like `history` does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from knowledge_forge.agent.providers.base import ToolSpec

PlanStatus = Literal["pending", "in_progress", "completed"]

_STATUS_MARK = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}


@dataclass
class PlanStep:
    content: str
    status: PlanStatus = "pending"


@dataclass
class PlanState:
    """Holds the agent's current plan for one session. Mutated in place by
    the `write_plan` tool handler so `AgentSession.plan` always reflects the
    latest call, even though `run_agent()` never returns it directly."""

    steps: list[PlanStep] = field(default_factory=list)

    def render(self) -> str:
        if not self.steps:
            return "(계획 없음)"
        return "\n".join(f"{_STATUS_MARK[s.status]} {s.content}" for s in self.steps)


PLAN_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "description": "현재 계획의 전체 단계 목록. 이전 write_plan 호출 내용을 통째로 대체한다 - 부분 수정이 아니다.",
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "단계 설명 - 한 문장, 실행 가능한 단위로 쓸 것."},
                    "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                },
                "required": ["content", "status"],
            },
        }
    },
    "required": ["steps"],
}


def _validate_steps(raw: Any) -> list[PlanStep]:
    if not isinstance(raw, list):
        raise ValueError("steps는 배열이어야 합니다.")
    steps = [PlanStep(content=str(item["content"]), status=item["status"]) for item in raw]
    if sum(1 for s in steps if s.status == "in_progress") > 1:
        raise ValueError("동시에 in_progress 상태인 단계는 하나만 허용됩니다.")
    return steps


def make_plan_tool(state: PlanState) -> ToolSpec:
    async def _handler(args: dict[str, Any]) -> str:
        state.steps = _validate_steps(args.get("steps", []))
        return f"계획이 갱신되었습니다:\n{state.render()}"

    return ToolSpec(
        name="write_plan",
        description=(
            "여러 단계로 이루어진 작업의 계획을 세우거나 갱신한다. 호출할 때마다 전체 단계 목록을 "
            "덮어쓰므로 이전에 적었던 단계까지 포함해 항상 전체 목록을 다시 보낼 것. 검색 1~2회로 "
            "바로 끝나는 단순 질문에는 쓰지 말 것."
        ),
        input_schema=PLAN_INPUT_SCHEMA,
        handler=_handler,
    )
