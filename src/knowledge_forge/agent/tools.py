"""Wraps `search.service`/`okf.fs_service`/`agent.memory` functions as
vendor-neutral `ToolSpec`s - the only thing this module does is declare a
JSON Schema and connect it to the underlying function; the actual logic
stays in those service layers (wiki/agent-runtime.md's "계층 설계": one
shared service, thin adapters on top). No SDK-specific decorator (no
`@tool`, no `create_sdk_mcp_server`) - `ToolSpec` (providers/base.py) is this
project's own type.

`delegate_task` (SubAgents) is the one tool here that isn't a thin wrapper
over a service function - it wraps `agent.loop.run_agent` itself, since
delegating IS running a nested agent turn to completion. See
`default_tools()`'s docstring for how recursion is prevented.
"""

from __future__ import annotations

from typing import Any

from knowledge_forge.agent.memory import write_memory
from knowledge_forge.agent.plan import PlanState, make_plan_tool
from knowledge_forge.agent.providers.base import ChatProvider, ToolSpec
from knowledge_forge.config import settings
from knowledge_forge.okf.fs_service import edit_file, list_dir, read_file, write_file
from knowledge_forge.search.planner import DEFAULT_LIMIT_PER_SUBQUERY, plan_and_search
from knowledge_forge.search.service import file_search, vector_search

FILE_SEARCH_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "검색어 - 정확한 고유명사/ID/에러 코드 등 리터럴 일치에 강하다."},
        "project_id": {
            "type": "string",
            "description": "특정 프로젝트로 범위를 좁힌다. 생략하면 OKF 트리의 모든 프로젝트를 검색한다.",
        },
        "type": {
            "type": "string",
            "description": "KnowledgeObject 타입(예: entity, decision)으로 결과를 좁힌다. 생략하면 모든 타입 + 리소스를 포함한다.",
        },
        "limit": {"type": "integer", "description": "반환할 최대 결과 수.", "default": 20},
    },
    "required": ["query"],
}

VECTOR_SEARCH_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "자연어 검색어 - 정확한 문자열 일치보다 패러프레이즈/의미 유사도에 강하다(file_search와 상호보완).",
        },
        "project_id": {
            "type": "string",
            "description": "특정 프로젝트로 범위를 좁힌다. 생략하면 모든 프로젝트를 검색한다.",
        },
        "type": {
            "type": "string",
            "description": "KnowledgeObject 타입(예: entity, decision)으로 결과를 좁힌다. 생략하면 모든 타입을 포함한다.",
        },
        "limit": {"type": "integer", "description": "반환할 최대 결과 수.", "default": 10},
    },
    "required": ["query"],
}

PLAN_SEARCH_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "여러 단계 추론이 필요한 복잡한 질문 - 하위 질의로 분해해 검색한다.",
        },
        "project_id": {
            "type": "string",
            "description": "특정 프로젝트로 범위를 좁힌다. 생략하면 세션 기본 project_id(있다면)를 쓴다.",
        },
        "limit_per_subquery": {
            "type": "integer",
            "description": "하위 질의 하나당 반환할 최대 결과 수.",
            "default": DEFAULT_LIMIT_PER_SUBQUERY,
        },
    },
    "required": ["query"],
}

READ_FILE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "OKF 루트 기준 상대 경로 - file_search 결과의 path를 그대로 쓸 것."},
    },
    "required": ["path"],
}

WRITE_FILE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "OKF 루트 기준 상대 경로. 없는 파일이면 새로 만들고, 있으면 통째로 덮어쓴다."},
        "content": {"type": "string", "description": "파일의 새 전체 내용."},
    },
    "required": ["path", "content"],
}

EDIT_FILE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "OKF 루트 기준 상대 경로 - 이미 존재하는 파일이어야 한다."},
        "old_text": {"type": "string", "description": "교체할 원문 - 파일 안에서 정확히 한 번만 일치해야 한다."},
        "new_text": {"type": "string", "description": "old_text를 대체할 새 텍스트."},
    },
    "required": ["path", "old_text", "new_text"],
}

LIST_DIR_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "OKF 루트 기준 상대 경로. 생략하면 OKF 루트(프로젝트 목록)를 나열한다.", "default": ""},
    },
}

WRITE_MEMORY_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "content": {"type": "string", "description": "기억에 남길 내용."},
        "mode": {
            "type": "string",
            "enum": ["append", "replace"],
            "default": "append",
            "description": "append(기본): 기존 기억을 유지하고 덧붙인다. replace: 기존 기억을 전부 지우고 이 내용으로 교체한다.",
        },
    },
    "required": ["content"],
}

DELEGATE_TASK_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "description": "서브에이전트에게 맡길 독립적인 하위 작업 - 서브에이전트는 지금까지의 대화를 보지 못하므로 필요한 맥락을 전부 포함해 구체적으로 쓸 것.",
        },
    },
    "required": ["task"],
}


async def _file_search_handler(args: dict[str, Any]) -> list[dict[str, Any]]:
    return await file_search(
        query=args["query"],
        project_id=args.get("project_id"),
        type=args.get("type"),
        limit=int(args.get("limit", 20)),
    )


async def _read_file_handler(args: dict[str, Any]) -> dict[str, Any]:
    return await read_file(path=args["path"])


async def _write_file_handler(args: dict[str, Any]) -> dict[str, Any]:
    return await write_file(path=args["path"], content=args["content"])


async def _edit_file_handler(args: dict[str, Any]) -> dict[str, Any]:
    return await edit_file(path=args["path"], old_text=args["old_text"], new_text=args["new_text"])


async def _list_dir_handler(args: dict[str, Any]) -> list[dict[str, Any]]:
    return await list_dir(path=args.get("path", ""))


def _make_vector_search_tool(database_url: str) -> ToolSpec:
    """`vector_search`는 `file_search`와 달리 자기 자신을 위한 Postgres 연결을
    묵시적으로 열 수 없다(OKF 루트처럼 그냥 경로를 읽는 게 아니라 커넥션이
    필요하므로) - 그래서 이 factory가 `database_url`을 클로저로 잡고, 호출마다
    `PipelineStore`를 열고 닫는다(`cli.py`의 `embed` 명령이 이미 쓰는
    "그때그때 연결" 패턴과 동일). 세션 전체에 커넥션 하나를 유지하는 대신
    호출마다 새로 여는 이유: 에이전트 도구 호출은 초당 여러 번 도는 핫 루프가
    아니라 검색 한 번에 짧은 지연을 감수할 수 있는 빈도라, 세션 lifecycle에
    커넥션 close를 새로 엮는 복잡도를 감수할 가치가 없다고 판단했다."""
    from knowledge_forge.storage.store import PipelineStore

    async def _handler(args: dict[str, Any]) -> list[dict[str, Any]]:
        async with PipelineStore(database_url, settings.data_dir) as store:
            return await vector_search(
                query=args["query"],
                store=store,
                project_id=args.get("project_id"),
                type=args.get("type"),
                limit=int(args.get("limit", 10)),
            )

    return ToolSpec(
        name="vector_search",
        description=(
            "KnowledgeObject 임베딩(pgvector)을 의미 기반으로 검색한다. "
            "file_search가 강한 정확한 문자열 일치 대신, 자연어 패러프레이즈/개념 질문에 강하다."
        ),
        input_schema=VECTOR_SEARCH_INPUT_SCHEMA,
        handler=_handler,
    )


def _make_plan_search_tool(*, provider: ChatProvider, database_url: str | None, project_id: str | None) -> ToolSpec:
    """`plan_search`(Query Planning + Query Routing, `search/planner.py`)는
    `vector_search`처럼 `database_url`이 있을 때만 vector_search 소스를 함께
    쓰지만, `database_url` 없이도(OKF 트리 + file_search만으로도) 동작한다는
    점이 다르다 - 그래서 이 도구 자체는 `provider`(하위 질의 분해용 LLM
    호출에 필요)만 있으면 추가되고, DB 유무는 `plan_and_search()` 내부에서
    사용 가능한 소스 목록을 좁히는 것으로만 반영된다."""
    from knowledge_forge.storage.store import PipelineStore

    async def _handler(args: dict[str, Any]) -> dict[str, Any]:
        scoped_project_id = args.get("project_id") or project_id
        limit = int(args.get("limit_per_subquery", DEFAULT_LIMIT_PER_SUBQUERY))
        if database_url:
            async with PipelineStore(database_url, settings.data_dir) as store:
                return await plan_and_search(
                    args["query"], provider, store=store, project_id=scoped_project_id, limit_per_subquery=limit
                )
        return await plan_and_search(
            args["query"], provider, store=None, project_id=scoped_project_id, limit_per_subquery=limit
        )

    return ToolSpec(
        name="plan_search",
        description=(
            "여러 단계 추론이 필요한 복잡한 질문을 하위 질의로 분해해, 서로 독립적인 하위 질의는 "
            "병렬로, 앞 결과에 의존하는 하위 질의는 그 결과를 반영해 검색한 뒤 근거 모음을 반환한다. "
            "검색 1~2회로 바로 답할 수 있는 단순 질문에는 쓰지 말고 file_search/vector_search를 "
            "직접 쓸 것 - 이 도구는 단순 질문에는 오히려 느리고 과하다."
        ),
        input_schema=PLAN_SEARCH_INPUT_SCHEMA,
        handler=_handler,
    )


def _make_write_memory_tool(project_id: str | None) -> ToolSpec:
    async def _handler(args: dict[str, Any]) -> str:
        return write_memory(content=args["content"], project_id=project_id, mode=args.get("mode", "append"))

    return ToolSpec(
        name="write_memory",
        description=(
            "이번 세션이 끝난 뒤에도 남겨야 할 사실/결정을 기억에 기록한다. 다음 세션에서는 이 내용이 "
            "시스템 프롬프트의 '이전 세션 기억'으로 자동 포함된다."
        ),
        input_schema=WRITE_MEMORY_INPUT_SCHEMA,
        handler=_handler,
    )


def _make_delegate_task_tool(
    *, project_id: str | None, provider: ChatProvider, system: str
) -> ToolSpec:
    """Builds `delegate_task` - runs a nested `run_agent()` call to
    completion and returns only its final answer text, never the child's
    intermediate tool calls/reasoning (hermes-agent's `delegate_task`
    context-isolation contract). Deliberately a local import of
    `agent.loop.run_agent`: `loop.py` doesn't import this module, so there
    is no real cycle, but importing it at module scope would still read as
    one - keeping it here makes "this is the one tool that recurses into
    the loop" visible at the call site."""
    from knowledge_forge.agent.loop import run_agent
    from knowledge_forge.agent.prompts import DELEGATE_SYSTEM_SUFFIX

    async def _handler(args: dict[str, Any]) -> str:
        child_tools = default_tools(project_id=project_id, provider=provider, system=system, include_delegate=False)
        answer, _ = await run_agent(
            args["task"],
            child_tools,
            system + DELEGATE_SYSTEM_SUFFIX,
            provider,
            max_iterations=settings.agent_max_iterations,
        )
        return answer

    return ToolSpec(
        name="delegate_task",
        description=(
            "독립적으로 완결 가능한 하위 작업을 새 대화로 시작하는 서브에이전트에게 위임하고 최종 결과 "
            "요약만 돌려받는다 - 검색 한두 번으로 끝나는 단순 작업에는 쓰지 말 것."
        ),
        input_schema=DELEGATE_TASK_INPUT_SCHEMA,
        handler=_handler,
    )


def default_tools(
    plan_state: PlanState | None = None,
    project_id: str | None = None,
    provider: ChatProvider | None = None,
    system: str | None = None,
    *,
    include_delegate: bool = True,
) -> list[ToolSpec]:
    """The full tool set: file_search + read_file/write_file/edit_file/
    list_dir (OKF-root-scoped, `okf/fs_service.py`) + write_memory
    (project-scoped, `agent/memory.py`) + write_plan, plus vector_search when
    `KF_DATABASE_URL` is configured, plan_search (Query Planning/Routing)
    when a `provider` is given, and delegate_task (SubAgents) when a
    `provider`/`system` are both given.

    `plan_state`를 넘기지 않으면 이 호출 하나에서만 쓰이고 버려지는 새
    `PlanState()`가 생긴다(run_query 같은 1회성 경로에 충분). `--interactive`
    REPL(agent/runtime.py::AgentSession)은 세션당 하나의 `PlanState`를 만들어
    넘겨서 turn 사이에 계획이 유지되게 한다 - `history`가 유지되는 것과 같은
    이유.

    `vector_search`는 `settings.database_url`(`KF_DATABASE_URL`)이 설정되어
    있을 때만 도구 목록에 추가된다 - `file_search`/파일시스템 도구와 달리
    Postgres+pgvector 없이는 아예 실행할 수 없는 도구라, `embedding_enabled`
    (`--embed`)와 같은 "설정 안 되어 있으면 조용히 빠짐" 컨벤션을 따른다.

    `plan_search`(Query Planning + Query Routing, `search/planner.py`)는
    `provider`가 주어졌을 때만 추가된다 - 하위 질의 분해에 그 `provider`로
    LLM 호출을 한 번 더 하기 때문(`delegate_task`와 같은 이유로 `provider`가
    필요, 다만 `system`은 필요 없다 - 분해 프롬프트는 자체 완결형). `vector_search`와
    달리 `KF_DATABASE_URL` 없이도 추가된다 - file_search만으로도 동작하고,
    DB 유무는 내부에서 사용 가능한 검색 소스 목록을 좁히는 것으로만 반영된다.

    `write_memory`는 `project_id`로 스코프된다 - `project_id`가 None이면
    "_global" 기억 파일에 쓴다(`agent/memory.py`).

    `delegate_task`(SubAgents)는 `provider`와 `system`이 둘 다 주어졌을 때만
    추가된다 - 자식 에이전트 턴을 실제로 돌리려면 둘 다 필요하기 때문이다.
    자식에게 넘기는 도구 집합은 `include_delegate=False`로 만든
    `default_tools()`를 그대로 쓴다 - **delegate_task 자신만 빠지고
    나머지(file_search/vector_search/plan_search/파일시스템/write_plan/
    write_memory)는 부모와 동일** - 이렇게 위임 한 단계 안에서 재귀적 위임을
    구조적으로 차단한다(자식의 도구 목록에는 애초에 delegate_task가 없으므로)."""
    tools = [
        ToolSpec(
            name="file_search",
            description=(
                "OKF 지식 베이스 파일 트리(마크다운+frontmatter)를 키워드로 검색한다. "
                "자연어 패러프레이즈보다 정확한 문자열 일치에 강하다."
            ),
            input_schema=FILE_SEARCH_INPUT_SCHEMA,
            handler=_file_search_handler,
        ),
    ]
    if settings.database_url:
        tools.append(_make_vector_search_tool(settings.database_url))
    if provider is not None:
        tools.append(_make_plan_search_tool(provider=provider, database_url=settings.database_url, project_id=project_id))
    tools.extend(
        [
            ToolSpec(
                name="read_file",
                description="OKF 루트 트리 내부의 파일 전체 내용을 읽는다. file_search 결과는 스니펫뿐이므로 전체가 필요할 때 쓴다.",
                input_schema=READ_FILE_INPUT_SCHEMA,
                handler=_read_file_handler,
            ),
            ToolSpec(
                name="write_file",
                description="OKF 루트 트리 내부에 새 파일을 만들거나 기존 파일을 통째로 덮어쓴다. 부분 수정에는 edit_file을 쓸 것.",
                input_schema=WRITE_FILE_INPUT_SCHEMA,
                handler=_write_file_handler,
            ),
            ToolSpec(
                name="edit_file",
                description="OKF 루트 트리 내부의 기존 파일에서 old_text 한 곳을 new_text로 교체한다(부분 수정).",
                input_schema=EDIT_FILE_INPUT_SCHEMA,
                handler=_edit_file_handler,
            ),
            ToolSpec(
                name="list_dir",
                description="OKF 루트 트리에서 한 디렉터리의 직계 항목(파일/하위 디렉터리)을 나열한다.",
                input_schema=LIST_DIR_INPUT_SCHEMA,
                handler=_list_dir_handler,
            ),
            _make_write_memory_tool(project_id),
            make_plan_tool(plan_state if plan_state is not None else PlanState()),
        ]
    )
    if include_delegate and provider is not None and system is not None:
        tools.append(_make_delegate_task_tool(project_id=project_id, provider=provider, system=system))
    return tools
