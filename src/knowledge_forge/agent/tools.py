"""Wraps `search.service` functions as vendor-neutral `ToolSpec`s - the only
thing this module does is declare a JSON Schema and connect it to the
underlying function; the search logic itself stays in `search/service.py`
(wiki/agent-runtime.md's "계층 설계": one shared service, thin adapters on
top). No SDK-specific decorator (no `@tool`, no `create_sdk_mcp_server`) -
`ToolSpec` (providers/base.py) is this project's own type.
"""

from __future__ import annotations

from typing import Any

from knowledge_forge.agent.providers.base import ToolSpec
from knowledge_forge.search.service import file_search

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


async def _file_search_handler(args: dict[str, Any]) -> list[dict[str, Any]]:
    return await file_search(
        query=args["query"],
        project_id=args.get("project_id"),
        type=args.get("type"),
        limit=int(args.get("limit", 20)),
    )


def default_tools() -> list[ToolSpec]:
    """Phase 1 tool set - file_search only (Postgres 불필요). vector_search/
    graph_search는 agent-search.md의 pgvector 후속 작업이 API 경로까지
    끝난 뒤 여기에 같은 패턴으로 추가된다 (wiki/agent-runtime.md Phase 2)."""
    return [
        ToolSpec(
            name="file_search",
            description=(
                "OKF 지식 베이스 파일 트리(마크다운+frontmatter)를 키워드로 검색한다. "
                "자연어 패러프레이즈보다 정확한 문자열 일치에 강하다."
            ),
            input_schema=FILE_SEARCH_INPUT_SCHEMA,
            handler=_file_search_handler,
        )
    ]
