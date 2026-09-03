import pytest

from knowledge_forge.agent.plan import PlanState, PlanStep, make_plan_tool


def test_render_reports_no_plan_when_empty():
    assert PlanState().render() == "(계획 없음)"


def test_render_marks_each_status():
    state = PlanState(
        steps=[
            PlanStep(content="a", status="completed"),
            PlanStep(content="b", status="in_progress"),
            PlanStep(content="c", status="pending"),
        ]
    )

    assert state.render() == "[x] a\n[~] b\n[ ] c"


async def test_handler_replaces_the_entire_step_list():
    state = PlanState(steps=[PlanStep(content="stale", status="completed")])
    tool = make_plan_tool(state)

    result = await tool.handler(
        {"steps": [{"content": "search project A", "status": "in_progress"}, {"content": "search project B", "status": "pending"}]}
    )

    assert [s.content for s in state.steps] == ["search project A", "search project B"]
    assert [s.status for s in state.steps] == ["in_progress", "pending"]
    assert "[~] search project A" in result
    assert "[ ] search project B" in result


async def test_handler_rejects_more_than_one_in_progress_step():
    state = PlanState()
    tool = make_plan_tool(state)

    with pytest.raises(ValueError, match="in_progress"):
        await tool.handler(
            {
                "steps": [
                    {"content": "a", "status": "in_progress"},
                    {"content": "b", "status": "in_progress"},
                ]
            }
        )

    # A failed write must not corrupt the previously accepted plan.
    assert state.steps == []


async def test_handler_accepts_an_empty_plan_to_clear_it():
    state = PlanState(steps=[PlanStep(content="stale", status="pending")])
    tool = make_plan_tool(state)

    await tool.handler({"steps": []})

    assert state.steps == []
