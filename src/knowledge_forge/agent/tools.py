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

from datetime import datetime
from typing import Any

from knowledge_forge.agent.memory import write_memory
from knowledge_forge.agent.plan import PlanState, make_plan_tool
from knowledge_forge.agent.providers.base import ChatProvider, ToolSpec
from knowledge_forge.config import settings
from knowledge_forge.okf.fs_service import edit_file, list_dir, read_file, write_file
from knowledge_forge.search.document import read_document
from knowledge_forge.search.planner import DEFAULT_LIMIT_PER_SUBQUERY, plan_and_search
from knowledge_forge.search.retrieve import retrieve
from knowledge_forge.search.service import document_search, file_search, lexical_search, vector_search
from knowledge_forge.search.sql import describe_schema, sql_search
from knowledge_forge.search.temporal import parse_temporal_expression

# 모델(혹은 색인된 문서에 심긴 프롬프트 인젝션 - agent/loop.py의 `_wrap_untrusted`
# 참고)이 limit을 비정상적으로 크게 불러 tool_result 하나가 컨텍스트 예산을
# 통째로 날려버리는 걸 막는 서버 사이드 상한 - search/sql.py::DEFAULT_MAX_ROWS와
# 같은 "모델이 부른 값을 그대로 믿지 않는다" 패턴. JSON Schema의 `maximum`은
# 잘 동작하는 모델에겐 힌트일 뿐이라, 핸들러에서도 반드시 clamp한다.
_MAX_SEARCH_LIMIT = 50
_MAX_PLAN_SEARCH_LIMIT_PER_SUBQUERY = 20

# `read_document`의 한 페이지 본문 글자 수 예산. 기본 20,000자는 개발 DB 기준 문서
# 대부분(평균 ~770자, 최장 ~21.6K자)을 한 번에 담으면서도 한국어 기준 대략 15~25K
# 토큰 - 컨텍스트 윈도우의 한 자리 수 % - 에 머무는 값이고, 상한 50,000자는 모델이
# 큰 값을 불러도 tool_result 하나가 예산을 통째로 날리지 못하게 막는다(위
# `_MAX_SEARCH_LIMIT`와 같은 "모델이 부른 값을 그대로 믿지 않는다" 패턴). 긴 문서는
# next_offset으로 이어 읽게 하는 편이 한 번에 다 싣는 것보다 낫다.
_READ_DOCUMENT_DEFAULT_MAX_CHARS = 20_000
_READ_DOCUMENT_MAX_CHARS = 50_000
# `search` 도구 결과의 히트당 본문 상한 - `postgres_store.py::_CHUNK_HIT_SUMMARY_CHARS`와 같은 500자.
_SEARCH_HIT_SUMMARY_CHARS = 500


def _clamp_limit(value: Any, default: int, maximum: int) -> int:
    try:
        limit = int(value) if value is not None else default
    except (TypeError, ValueError):
        limit = default
    return max(1, min(limit, maximum))


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
        "limit": {
            "type": "integer",
            "description": f"반환할 최대 결과 수 (서버가 {_MAX_SEARCH_LIMIT}로 상한을 둔다).",
            "default": 20,
            "maximum": _MAX_SEARCH_LIMIT,
        },
        "time_range": {
            "type": "string",
            "description": (
                "자연어 시간 표현으로 결과를 좁힌다 (예: '어제', '지난주', '지난달', '이번 분기', "
                "'2025년 Q3', '최근 3일'). 이해하지 못하는 표현이면 무시된다(에러 아님)."
            ),
        },
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
        "limit": {
            "type": "integer",
            "description": f"반환할 최대 결과 수 (서버가 {_MAX_SEARCH_LIMIT}로 상한을 둔다).",
            "default": 10,
            "maximum": _MAX_SEARCH_LIMIT,
        },
        "time_range": {
            "type": "string",
            "description": (
                "자연어 시간 표현으로 결과를 좁힌다 (예: '어제', '지난주', '지난달', '이번 분기', "
                "'2025년 Q3', '최근 3일'). 이해하지 못하는 표현이면 무시된다(에러 아님)."
            ),
        },
    },
    "required": ["query"],
}

_TIME_RANGE_PROPERTY: dict[str, Any] = {
    "type": "string",
    "description": (
        "자연어 시간 표현으로 결과를 좁힌다 (예: '어제', '지난주', '지난달', '이번 분기', "
        "'2025년 Q3', '최근 3일'). 이해하지 못하는 표현이면 무시된다(에러 아님)."
    ),
}

DOCUMENT_SEARCH_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "찾는 문서의 제목/파일명(또는 그 일부) - 예: '2025_SH_업무_보고서', 'SH 보고서', "
                "'산출물 기간 변경'. 자연어 문장이 아니라 이름만 넣을 것."
            ),
        },
        "project_id": {
            "type": "string",
            "description": "특정 프로젝트로 범위를 좁힌다. 생략하면 모든 프로젝트를 검색한다.",
        },
        "source_type": {
            "type": "string",
            "description": "소스 종류로 좁힌다: local_file/slack/gmail/google_drive/notion. 생략하면 전부.",
        },
        "limit": {
            "type": "integer",
            "description": f"반환할 최대 후보 수 (서버가 {_MAX_SEARCH_LIMIT}로 상한을 둔다).",
            "default": 10,
            "maximum": _MAX_SEARCH_LIMIT,
        },
        "time_range": _TIME_RANGE_PROPERTY,
    },
    "required": ["query"],
}

LEXICAL_SEARCH_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "원문에 그대로 들어 있어야 하는 문자열 - 에러 코드('ERR_CONNECTION_RESET'), 티켓/문서 ID, "
                "정확한 문구. 의미가 비슷한 표현은 찾지 못하므로 그런 경우엔 vector_search를 쓸 것."
            ),
        },
        "project_id": {
            "type": "string",
            "description": "특정 프로젝트로 범위를 좁힌다. 생략하면 모든 프로젝트를 검색한다.",
        },
        "limit": {
            "type": "integer",
            "description": f"반환할 최대 결과 수 (서버가 {_MAX_SEARCH_LIMIT}로 상한을 둔다).",
            "default": 10,
            "maximum": _MAX_SEARCH_LIMIT,
        },
        "time_range": _TIME_RANGE_PROPERTY,
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
            "description": (
                f"하위 질의 하나당 반환할 최대 결과 수 (서버가 {_MAX_PLAN_SEARCH_LIMIT_PER_SUBQUERY}로 "
                "상한을 둔다 - 최대 5개 하위 질의 x 최대 2개 소스에 곱해지므로 file_search/vector_search "
                "보다 낮게 잡혀 있다)."
            ),
            "default": DEFAULT_LIMIT_PER_SUBQUERY,
            "maximum": _MAX_PLAN_SEARCH_LIMIT_PER_SUBQUERY,
        },
    },
    "required": ["query"],
}

READ_DOCUMENT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "document_id": {
            "type": "string",
            "description": "읽을 문서의 document_id - search 결과의 document_id를 그대로 쓸 것.",
        },
        "offset": {
            "type": "integer",
            "description": "이어 읽기를 시작할 청크 인덱스 - 이전 결과의 next_offset을 그대로 넘긴다. 생략하면 처음부터.",
            "default": 0,
        },
        "max_chars": {
            "type": "integer",
            "description": (
                f"한 번에 돌려줄 본문 글자 수 예산 (기본 {_READ_DOCUMENT_DEFAULT_MAX_CHARS}, 서버가 "
                f"{_READ_DOCUMENT_MAX_CHARS}로 상한을 둔다). 예산이 차면 truncated: true와 next_offset이 돌아온다."
            ),
            "default": _READ_DOCUMENT_DEFAULT_MAX_CHARS,
            "maximum": _READ_DOCUMENT_MAX_CHARS,
        },
    },
    "required": ["document_id"],
}

SEARCH_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "사용자의 질문을 자연어 그대로 - 시간 표현('지난주'), 파일명/에러 코드, '요약해줘'/'전부'/"
                "'몇 건' 같은 의도 표현을 지우지 말고 넣을 것(그걸 보고 전략을 고른다)."
            ),
        },
        "project_id": {
            "type": "string",
            "description": "특정 프로젝트로 범위를 좁힌다. 생략하면 세션 기본 project_id(있다면)를 쓴다.",
        },
        "limit": {
            "type": "integer",
            "description": f"반환할 최대 문서 수 (서버가 {_MAX_SEARCH_LIMIT}로 상한을 둔다).",
            "default": 10,
            "maximum": _MAX_SEARCH_LIMIT,
        },
    },
    "required": ["query"],
}

SQL_SEARCH_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sql": {
            "type": "string",
            "description": "실행할 읽기 전용 SQL(SELECT 또는 WITH ... SELECT). 도구 설명의 스키마를 참고해 작성할 것.",
        },
    },
    "required": ["sql"],
}

READ_FILE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "OKF 루트 기준 상대 경로 - file_search/list_dir 결과의 path를 그대로 쓸 것."},
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


def _resolve_time_range(args: dict[str, Any]) -> tuple[Any, Any]:
    """Shared by the `file_search`/`vector_search` tool handlers - turns the
    optional natural-language `time_range` argument into `(since, until)` via
    `search/temporal.py::parse_temporal_expression`. An expression it doesn't
    recognize resolves to `(None, None)` (no filter applied) rather than an
    error - the model guessing at a time expression this parser doesn't cover
    should degrade to "search everything", not fail the whole tool call."""
    time_range = args.get("time_range")
    if not time_range:
        return None, None
    parsed = parse_temporal_expression(time_range)
    if parsed is None:
        return None, None
    return parsed.start, parsed.end


async def _file_search_handler(args: dict[str, Any]) -> list[dict[str, Any]]:
    since, until = _resolve_time_range(args)
    return await file_search(
        query=args["query"],
        project_id=args.get("project_id"),
        type=args.get("type"),
        limit=_clamp_limit(args.get("limit"), default=20, maximum=_MAX_SEARCH_LIMIT),
        since=since,
        until=until,
    )


async def _read_file_handler(args: dict[str, Any]) -> dict[str, Any]:
    return await read_file(path=args["path"])


async def _write_file_handler(args: dict[str, Any]) -> dict[str, Any]:
    return await write_file(path=args["path"], content=args["content"])


async def _edit_file_handler(args: dict[str, Any]) -> dict[str, Any]:
    return await edit_file(path=args["path"], old_text=args["old_text"], new_text=args["new_text"])


async def _list_dir_handler(args: dict[str, Any]) -> list[dict[str, Any]]:
    return await list_dir(path=args.get("path", ""))


def _make_file_search_tool() -> ToolSpec:
    """`file_search`(`search/service.py`)는 Postgres를 전혀 요구하지 않는 유일한
    검색 도구다 - OKF 루트 트리를 그냥 읽는다. 그래서 `KF_DATABASE_URL`이 없는
    배포에서는 이것 하나가 남는 검색 경로이고, `default_tools()`의 "minimal"
    표면은 정확히 그 경우에만 이걸 넣는다(자세한 이유는 그쪽 docstring).
    다른 도구들처럼 factory로 뽑아 둔 것은 조립부에서 조건별로 같은 모양으로
    다루기 위해서다."""
    return ToolSpec(
        name="file_search",
        description=(
            "OKF 지식 베이스 파일 트리(마크다운+frontmatter)를 키워드로 검색한다 - Postgres 없이 "
            "동작하는 유일한 검색 도구라, search가 도구 목록에 없는 배포에서는 이것이 기본 검색 "
            "경로다. 자연어 패러프레이즈보다 정확한 문자열 일치에 강하다."
        ),
        input_schema=FILE_SEARCH_INPUT_SCHEMA,
        handler=_file_search_handler,
    )


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
        since, until = _resolve_time_range(args)
        async with PipelineStore(database_url, settings.data_dir) as store:
            return await vector_search(
                query=args["query"],
                store=store,
                project_id=args.get("project_id"),
                type=args.get("type"),
                limit=_clamp_limit(args.get("limit"), default=10, maximum=_MAX_SEARCH_LIMIT),
                since=since,
                until=until,
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


def _make_read_document_tool(database_url: str) -> ToolSpec:
    """`read_document`(document expansion, `search/document.py`)는
    `vector_search`와 같은 이유로 `KF_DATABASE_URL`이 있을 때만 추가되고
    호출마다 `PipelineStore`를 열고 닫는다. 검색 도구들이 top-k 청크/KO만
    돌려주는 것과 달리 문서 하나의 청크 **전체**를 순서대로 돌려준다 - 문서
    전체 요약, "보고서에서 리스크만 전부", "처음 제안 vs 최종 결정", "언급되지
    않은 것" 같은 질의는 상위 k건으로는 답할 수 없기 때문. 없는 document_id는
    `read_file`의 FileNotFoundError와 같은 방식으로 예외를 던져 `agent/loop.py`가
    "error: ..." tool_result로 바꾸게 한다 - 모델이 잘못 짚은 ID를 다시 검색하게
    만드는 신호로는 그게 가장 분명하다."""
    from knowledge_forge.storage.store import PipelineStore

    async def _handler(args: dict[str, Any]) -> dict[str, Any]:
        document_id = args["document_id"]
        try:
            offset = max(0, int(args.get("offset") or 0))
        except (TypeError, ValueError):
            offset = 0
        max_chars = _clamp_limit(
            args.get("max_chars"), default=_READ_DOCUMENT_DEFAULT_MAX_CHARS, maximum=_READ_DOCUMENT_MAX_CHARS
        )
        async with PipelineStore(database_url, settings.data_dir) as store:
            result = await read_document(document_id, store, max_chars=max_chars, offset=offset)
        if result is None:
            raise LookupError(f"document not found: {document_id}")
        return result

    return ToolSpec(
        name="read_document",
        description=(
            "search로 document_id를 특정한 뒤, 문서 전체 요약·문서 내부 질문·"
            "부재 확인('언급되지 않은 것은?')·순서 비교('처음 제안 vs 최종 결정')처럼 검색 top-k로는 답할 수 "
            "없는 질문에 쓴다 - 그 문서의 청크 전체를 원문 순서(sequence_index)로 돌려준다. 단순 사실 "
            "검색에는 쓰지 말 것. 긴 문서는 truncated: true와 next_offset이 돌아오므로 offset에 그 값을 "
            "넣어 이어 읽을 것."
        ),
        input_schema=READ_DOCUMENT_INPUT_SCHEMA,
        handler=_handler,
    )


def _make_search_tool(
    database_url: str, *, provider: ChatProvider | None, project_id: str | None, now: datetime | None = None
) -> ToolSpec:
    """`search`(통합 검색, `search/retrieve.py`) - `vector_search`와 같은 "database_url이 있을
    때만, 호출마다 `PipelineStore`를 열고 닫는" 패턴. 개별 도구(document/lexical/vector/sql/
    read_document)를 모델이 고르는 대신 질의 제약(`search/constraints.py`)으로 전략을 결정론적으로
    조합한다 - 평가 하네스가 쓰는 것과 같은 경로라, 여기서 좋아진 것은 평가 지표에도 그대로
    반영된다. `provider`가 있으면 제약 보강과 멀티홉 분해에 쓴다(없으면 규칙만).

    결과는 `RetrievalResult.to_dict()`에서 hits 상위 limit개 + steps 요약만 싣는다 - 청크 원문은
    검색 소스가 이미 500자로 자르지만(`postgres_store.py::_CHUNK_HIT_SUMMARY_CHARS`) 여기서도
    같은 상한을 다시 건다(`_MAX_SEARCH_LIMIT`와 같은 "값을 그대로 믿지 않는다" 패턴)."""
    from knowledge_forge.storage.store import PipelineStore

    async def _handler(args: dict[str, Any]) -> dict[str, Any]:
        limit = _clamp_limit(args.get("limit"), default=10, maximum=_MAX_SEARCH_LIMIT)
        async with PipelineStore(database_url, settings.data_dir) as store:
            result = await retrieve(
                args["query"],
                store=store,
                provider=provider,
                project_id=args.get("project_id") or project_id,
                limit=limit,
                # 평가 하네스가 골든셋의 as_of를 내려보낼 때만 값이 있다. 실사용에서는
                # None이라 `retrieve()`가 지금 시각을 쓴다 - "어제"의 기준을 호출자가
                # 정할 수 있어야 상대 시간 질의를 재현 가능하게 채점할 수 있다.
                now=now,
            )
        payload = result.to_dict()
        hits = []
        for hit in payload["hits"][:limit]:
            summary = hit.get("summary")
            if isinstance(summary, str) and len(summary) > _SEARCH_HIT_SUMMARY_CHARS:
                hit["summary"] = summary[:_SEARCH_HIT_SUMMARY_CHARS]
            hits.append(hit)
        return {
            "strategies": payload["strategies"],
            "constraints": payload["constraints"],
            "steps": [
                {"strategy": s["strategy"], "source": s["source"], "n_hits": s["n_hits"], "detail": s["detail"]}
                for s in payload["steps"]
            ],
            "document_ids": payload["document_ids"][:limit],
            "documents_read": payload["documents_read"],
            "predicted_count": payload["predicted_count"],
            "hits": hits,
        }

    return ToolSpec(
        name="search",
        description=(
            "질의 의도를 분석해 제목/렉시컬/벡터/메타데이터/문서 전체 읽기 전략을 자동으로 조합하는 통합 검색 - "
            "어떤 도구를 쓸지 모르겠으면 이것부터. 질문을 자연어 그대로 넣으면 시간 표현·파일명·에러 코드·"
            "'요약해줘'/'전부'/'몇 건' 같은 의도를 읽어 필요한 검색을 골라 실행하고, 결과(document_id/"
            "chunk_id/summary)와 함께 어떤 전략을 어떻게 돌렸는지(steps)를 돌려준다. 결과가 부족하면 "
            "steps를 보고 read_document(문서 전체 읽기)/sql_search(집계·속성 조건)로 보충할 것 - "
            "그 둘로도 부족할 때만 도구 목록에 있는 개별 검색 도구를 쓸 것."
        ),
        input_schema=SEARCH_INPUT_SCHEMA,
        handler=_handler,
    )


def _make_document_search_tool(database_url: str) -> ToolSpec:
    """`document_search`(`search/service.py`) - `vector_search`와 같은
    "database_url이 있을 때만, 호출마다 `PipelineStore`를 열고 닫는" 패턴.
    별도 도구로 두는 이유: "2025_SH_업무_보고서 찾아줘" 같은 Navigational 질의는
    답이 *문서 하나(또는 후보 몇 개)* 이지 청크/KO가 아니라, 결과 모양 자체가
    다르다(본문 없이 제목+document_id 목록). `file_search`/`vector_search`
    결과에 섞어 넣으면 모델이 "이게 그 문서인지"를 특정하는 단계를 건너뛰고
    스니펫부터 읽기 시작한다."""
    from knowledge_forge.storage.store import PipelineStore

    async def _handler(args: dict[str, Any]) -> list[dict[str, Any]]:
        since, until = _resolve_time_range(args)
        async with PipelineStore(database_url, settings.data_dir) as store:
            return await document_search(
                query=args["query"],
                store=store,
                project_id=args.get("project_id"),
                source_type=args.get("source_type"),
                limit=_clamp_limit(args.get("limit"), default=10, maximum=_MAX_SEARCH_LIMIT),
                since=since,
                until=until,
            )

    return ToolSpec(
        name="document_search",
        description=(
            "문서를 제목/파일명으로 찾아 후보 목록(document_id, title, source_type, uri, modified_at, score)을 "
            "반환한다 - '2025_SH_업무_보고서 찾아줘', 'SH 보고서'처럼 **어떤 문서인지 특정**해야 할 때 "
            "가장 먼저 쓸 것(부분 일치/오타도 허용, 후보가 여러 개면 전부 반환하니 사용자에게 어느 것인지 "
            "되물을 수 있다). 본문은 싣지 않는다 - 특정한 document_id로 lexical_search/sql_search를 이어갈 것. "
            "내용/개념으로 찾는 질문에는 vector_search가 적합하다."
        ),
        input_schema=DOCUMENT_SEARCH_INPUT_SCHEMA,
        handler=_handler,
    )


def _make_lexical_search_tool(database_url: str) -> ToolSpec:
    """`lexical_search`(`search/service.py`) - `file_search`가 OKF 마크다운(추출된
    KO 요약)만 보는 것과 달리 Postgres `chunks.text` 원문을 pg_trgm으로 보므로,
    추출 과정에서 요약되며 사라진 에러 코드/ID 같은 리터럴도 찾는다."""
    from knowledge_forge.storage.store import PipelineStore

    async def _handler(args: dict[str, Any]) -> list[dict[str, Any]]:
        since, until = _resolve_time_range(args)
        async with PipelineStore(database_url, settings.data_dir) as store:
            return await lexical_search(
                query=args["query"],
                store=store,
                project_id=args.get("project_id"),
                limit=_clamp_limit(args.get("limit"), default=10, maximum=_MAX_SEARCH_LIMIT),
                since=since,
                until=until,
            )

    return ToolSpec(
        name="lexical_search",
        description=(
            "원문 청크(chunks.text)에서 문자열이 그대로 들어 있는 곳을 찾는다 - 에러 코드, 티켓/문서 ID, "
            "정확한 문구처럼 리터럴 일치가 필요한 질의용. 결과는 vector_search와 같은 모양(document_id, "
            "chunk_id, 일치 지점 주변 summary, score)이다. 패러프레이즈/개념 질문에는 vector_search를, "
            "문서 자체를 제목으로 찾을 때는 document_search를 쓸 것."
        ),
        input_schema=LEXICAL_SEARCH_INPUT_SCHEMA,
        handler=_handler,
    )


def _make_sql_search_tool(database_url: str) -> ToolSpec:
    """`sql_search`(Text-to-SQL, `search/sql.py`)는 `vector_search`와 같은
    이유로 `KF_DATABASE_URL`이 있을 때만 추가되고 호출마다 `PipelineStore`를
    열고 닫는다. `file_search`/`vector_search`가 다루기 어려운 구조화된
    질문(특정 사람이 언급된 항목, 여러 조건의 AND/OR, 집계, 시간 범위 +
    속성값을 함께 거는 temporal join)을 모델이 직접 쓴 SQL로 풀게 한다 -
    별도 자연어→SQL 변환 LLM 호출 없이, 이 도구를 호출하는 모델 자신이
    이미 그 번역을 한다(도구 설명에 스키마를 실어 보내는 것만으로 충분).
    안전장치(읽기 전용 강제, 행 수 제한)는 `search/sql.py`/
    `storage/postgres_store.py::execute_readonly_query`에 있다."""
    from knowledge_forge.storage.store import PipelineStore

    async def _handler(args: dict[str, Any]) -> dict[str, Any]:
        async with PipelineStore(database_url, settings.data_dir) as store:
            return await sql_search(args["sql"], store)

    return ToolSpec(
        name="sql_search",
        description=(
            "Knowledge Base를 PostgreSQL SQL로 직접 조회한다(Text-to-SQL) - search가 다루지 못하는 "
            "구조화된 질문(특정 사람/속성이 언급된 항목, 여러 조건의 AND/OR, 집계(COUNT 등), "
            "시간 범위와 속성값을 함께 거는 조건)에 쓸 것. 키워드/의미 검색이나 문서 찾기에는 "
            "search가 더 빠르고 적합하다 - 먼저 search를 부르고, 그걸로 안 되는 것만 여기서 풀 것. 읽기 전용만 "
            f"허용된다(쓰기/DDL은 거부).\n\n{describe_schema()}"
        ),
        input_schema=SQL_SEARCH_INPUT_SCHEMA,
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
        limit = _clamp_limit(
            args.get("limit_per_subquery"),
            default=DEFAULT_LIMIT_PER_SUBQUERY,
            maximum=_MAX_PLAN_SEARCH_LIMIT_PER_SUBQUERY,
        )
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
    now: datetime | None = None,
) -> list[ToolSpec]:
    """도구 집합 조립. **검색 도구 표면은 `settings.agent_tool_surface`
    (`KF_AGENT_TOOL_SURFACE`, 기본 "minimal")가 정한다** - 나머지(파일시스템/
    write_memory/write_plan/delegate_task)는 표면과 무관하게 항상 같다.

    "minimal"(기본) + `KF_DATABASE_URL` 설정됨:
        search, read_document, sql_search + 비검색 도구
    "minimal" + DB 없음(OKF 전용 배포):
        file_search + 비검색 도구
    "full" + DB 설정됨:
        search, file_search, read_document, sql_search, vector_search,
        document_search, lexical_search, plan_search(provider 있을 때) + 비검색 도구
    "full" + DB 없음:
        file_search, plan_search(provider 있을 때) + 비검색 도구

    왜 기본을 좁히는가: `search/retrieve.py`는 "어떤 검색을 어떤 순서로 돌릴지"를
    LLM에게서 빼앗아 결정론적 함수로 만들려고 존재하는 모듈이고(그 docstring),
    골든셋 IR 평가가 재는 유일한 경로다(wiki/search-evaluation.md). 그런데 도구
    목록이 개별 검색 도구를 여덟 개 늘어놓으면 그 결정을 그대로 모델에게 되돌려
    주는 셈이라 - 모델이 고른 조합은 측정된 적이 없고 재현되지도 않는다.
    `sql_search`만 예외로 남긴 이유는 집계(COUNT/GROUP BY)와 임의 속성 조건이
    `search`의 전략 목록에 없어서, 그게 필요한 질문은 다른 도구로는 아예 답이
    안 나오기 때문이다(구조화 질의 비상구). `read_document`는 검색 도구가 아니라
    검색으로 특정한 문서를 끝까지 읽는 후속 동작이라 남는다.

    `file_search`가 "minimal"에서 DB가 없을 때만 들어가는 것도 같은 이유의 뒷면
    이다: 그 구성에서는 `search`/`sql_search`/`read_document`가 전부 빠져
    (Postgres 없이는 실행 자체가 불가능하므로 - `embedding_enabled`와 같은
    "설정 안 되어 있으면 조용히 빠짐" 컨벤션) 도구 목록에 검색 경로가 하나도
    남지 않는다. `file_search`는 Postgres를 전혀 요구하지 않는 유일한 검색
    경로이므로(`search/service.py` docstring의 "OKF-only deployment") 그때는
    반드시 남아야 한다. 반대로 DB가 있으면 `search`가 같은 OKF 코퍼스를 색인한
    Postgres 쪽을 더 넓게 훑으므로 기본 표면에서는 뺀다 - `KF_OKF_DIR`이 안
    잡힌 배포에서 `file_search`는 조용히 `[]`만 돌려주기까지 한다.

    `plan_search`(Query Planning + Query Routing, `search/planner.py`)는 "full"
    에서만, 그것도 `provider`가 주어졌을 때만 추가된다 - 하위 질의 분해에 그
    `provider`로 LLM 호출을 한 번 더 하기 때문(`delegate_task`와 같은 이유로
    `provider`가 필요, 다만 `system`은 필요 없다 - 분해 프롬프트는 자체 완결형).
    "minimal"에서 빠져도 멀티홉 분해 자체는 사라지지 않는다: `search`가
    `multihop` 전략으로 같은 `plan_and_search()`를 내부에서 부른다.

    `plan_state`를 넘기지 않으면 이 호출 하나에서만 쓰이고 버려지는 새
    `PlanState()`가 생긴다(run_query 같은 1회성 경로에 충분). `--interactive`
    REPL(agent/runtime.py::AgentSession)은 세션당 하나의 `PlanState`를 만들어
    넘겨서 turn 사이에 계획이 유지되게 한다 - `history`가 유지되는 것과 같은
    이유.

    `write_memory`는 `project_id`로 스코프된다 - `project_id`가 None이면
    "_global" 기억 파일에 쓴다(`agent/memory.py`).

    `delegate_task`(SubAgents)는 `provider`와 `system`이 둘 다 주어졌을 때만
    추가된다 - 자식 에이전트 턴을 실제로 돌리려면 둘 다 필요하기 때문이다.
    자식에게 넘기는 도구 집합은 `include_delegate=False`로 만든
    `default_tools()`를 그대로 쓴다 - **delegate_task 자신만 빠지고 나머지는
    (같은 표면 설정 아래에서) 부모와 동일** - 이렇게 위임 한 단계 안에서
    재귀적 위임을 구조적으로 차단한다(자식의 도구 목록에는 애초에
    delegate_task가 없으므로)."""
    minimal = settings.agent_tool_surface != "full"
    database_url = settings.database_url
    tools: list[ToolSpec] = []
    if database_url:
        # 1순위 검색 도구를 목록 맨 앞에 둔다 - 도구 순서는 모델이 읽는 순서이기도 하다.
        tools.append(_make_search_tool(database_url, provider=provider, project_id=project_id, now=now))
    if not database_url or not minimal:
        tools.append(_make_file_search_tool())
    if database_url:
        tools.append(_make_read_document_tool(database_url))
        tools.append(_make_sql_search_tool(database_url))
        if not minimal:
            tools.append(_make_vector_search_tool(database_url))
            tools.append(_make_document_search_tool(database_url))
            tools.append(_make_lexical_search_tool(database_url))
    if provider is not None and not minimal:
        tools.append(_make_plan_search_tool(provider=provider, database_url=database_url, project_id=project_id))
    tools.extend(
        [
            ToolSpec(
                name="read_file",
                description="OKF 루트 트리 내부의 파일 전체 내용을 읽는다. 검색 결과는 스니펫뿐이므로 전체가 필요할 때 쓴다.",
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
