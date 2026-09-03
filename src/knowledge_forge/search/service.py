"""Keyword/frontmatter search over an OKF file tree - the `file_search` tool
from wiki/agent-search.md. Postgres-free by design (unlike vector_search/
graph_search, still design-only) so it works on an OKF-only deployment and
needs no new infrastructure - see wiki/agent-search.md's "왜 세 가지 도구로
나누는가".

This is a plain async function, not a class, and imports nothing from
FastAPI/MCP/the agent harness - wiki/agent-runtime.md's "계층 설계"
principle is that every consumer (API route, MCP server, agent tool) is a
thin adapter over this function, never the other way around.

Reuses `okf.writer._parse_markdown` the same way `okf/reader.py` already
does, so this never drifts from the frontmatter shape `OKFWriter` actually
produces (see reader.py's own module docstring).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

from knowledge_forge.config import settings
from knowledge_forge.okf.reader import list_projects
from knowledge_forge.okf.writer import _parse_markdown

_RESOURCES_DIR = "resources"
_OBJECTS_DIR = "objects"
_INDEX_FILE = "index.md"
_SNIPPET_RADIUS = 160

# Field weights for ranking - a hit in the title/description/tags is a
# stronger signal than the same substring showing up somewhere in the body,
# so results are sorted by the best weight they matched at (ties broken by
# path for deterministic output, since there is no embedding-based score to
# break ties with here).
_TITLE_WEIGHT = 3
_DESCRIPTION_OR_TAGS_WEIGHT = 2
_BODY_WEIGHT = 1


def _resolve_okf_root(okf_root: str | Path | None) -> Path | None:
    root = okf_root if okf_root is not None else settings.okf_dir
    return Path(root) if root else None


def _project_ids(root: Path, project_id: str | None) -> list[str]:
    if project_id:
        return [project_id]
    return list_projects(root)


def _iter_candidate_files(project_root: Path, type_filter: str | None) -> Iterator[tuple[Path, str | None]]:
    """Yields (path, ko_type) pairs to scan. `type_filter` is matched against
    the `objects/{type}/` directory name (the KnowledgeObjectType enum value,
    e.g. "entity"/"decision") rather than the title-cased frontmatter `type`
    string, since the directory name is the stable machine-readable key -
    resources/ (a different, source-level `type`) is skipped entirely when a
    KO type filter is given, since it can never match one."""
    if type_filter is None:
        resources_dir = project_root / _RESOURCES_DIR
        if resources_dir.is_dir():
            for f in sorted(resources_dir.glob("*.md")):
                if f.name != _INDEX_FILE:
                    yield f, None

    objects_dir = project_root / _OBJECTS_DIR
    if not objects_dir.is_dir():
        return
    for type_dir in sorted(objects_dir.iterdir()):
        if not type_dir.is_dir():
            continue
        if type_filter and type_dir.name != type_filter:
            continue
        for f in sorted(type_dir.glob("*.md")):
            if f.name != _INDEX_FILE:
                yield f, type_dir.name


def _best_match_weight(frontmatter: dict[str, Any], body: str, query_lower: str) -> int | None:
    """Returns the best field weight `query_lower` matched at, or None if it
    matched nowhere - substring match, case-insensitive, no tokenization
    (good enough for exact-ish literal queries; agent-search.md's
    vector_search is what covers paraphrase/semantic recall)."""
    title = frontmatter.get("title") or ""
    description = frontmatter.get("description") or ""
    tags = frontmatter.get("tags") or []
    tags_text = " ".join(tags) if isinstance(tags, list) else str(tags)

    best: int | None = None
    if query_lower in title.lower():
        best = _TITLE_WEIGHT
    if best != _TITLE_WEIGHT and (query_lower in description.lower() or query_lower in tags_text.lower()):
        best = _DESCRIPTION_OR_TAGS_WEIGHT
    if best is None and query_lower in body.lower():
        best = _BODY_WEIGHT
    return best


def _snippet(frontmatter: dict[str, Any], body: str, query_lower: str) -> str:
    """A window of body text centered on the first match, falling back to the
    frontmatter description (then the start of the body) when the query only
    matched title/tags and not the body itself."""
    body_lower = body.lower()
    idx = body_lower.find(query_lower)
    if idx == -1:
        description = frontmatter.get("description")
        if description:
            return description
        return body[:_SNIPPET_RADIUS].strip()

    start = max(0, idx - _SNIPPET_RADIUS // 2)
    end = min(len(body), idx + len(query_lower) + _SNIPPET_RADIUS // 2)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(body) else ""
    return f"{prefix}{body[start:end].strip()}{suffix}"


async def file_search(
    query: str,
    project_id: str | None = None,
    type: str | None = None,
    limit: int = 20,
    okf_root: str | Path | None = None,
) -> list[dict[str, Any]]:
    """OKF 파일 트리 위의 키워드/frontmatter 검색 - wiki/agent-search.md의 file_search 계약.

    `okf_root`를 생략하면 `KF_OKF_DIR`(settings.okf_dir)을 쓴다 - OKF 트리가 설정되어 있지 않거나
    존재하지 않으면 빈 리스트를 반환한다(에러가 아님 - 도구 호출 루프에서 "결과 없음"과 동일하게
    다뤄지면 되는 상황).
    """
    root = _resolve_okf_root(okf_root)
    query_lower = query.strip().lower()
    if not query_lower or root is None or not root.is_dir():
        return []

    hits: list[tuple[int, dict[str, Any]]] = []
    for pid in _project_ids(root, project_id):
        project_root = root / pid
        if not project_root.is_dir():
            continue
        for path, ko_type in _iter_candidate_files(project_root, type):
            frontmatter, body = _parse_markdown(path.read_text(encoding="utf-8"))
            weight = _best_match_weight(frontmatter, body, query_lower)
            if weight is None:
                continue
            tags = frontmatter.get("tags") or []
            hits.append(
                (
                    weight,
                    {
                        "path": str(path.relative_to(root)),
                        "title": frontmatter.get("title"),
                        "type": ko_type or frontmatter.get("type"),
                        "tags": tags if isinstance(tags, list) else [],
                        "snippet": _snippet(frontmatter, body, query_lower),
                        "project_id": pid,
                    },
                )
            )

    hits.sort(key=lambda pair: (-pair[0], pair[1]["path"]))
    return [hit for _, hit in hits[:limit]]
