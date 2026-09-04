"""Search tools over the knowledge base - `file_search` (OKF file tree) and
`vector_search` (pgvector) from wiki/agent-search.md. `file_search` is
Postgres-free by design so it works on an OKF-only deployment and needs no
new infrastructure - see wiki/agent-search.md's "왜 세 가지 도구로 나누는가"
(`graph_search`, the third leg, is still design-only).

Both are plain async functions, not classes, and import nothing from
FastAPI/MCP/the agent harness - wiki/agent-runtime.md's "계층 설계"
principle is that every consumer (API route, MCP server, agent tool) is a
thin adapter over these functions, never the other way around.

`file_search` reuses `okf.writer._parse_markdown` the same way `okf/reader.py`
already does, so this never drifts from the frontmatter shape `OKFWriter`
actually produces (see reader.py's own module docstring).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

from knowledge_forge.config import settings
from knowledge_forge.okf.reader import list_projects
from knowledge_forge.okf.writer import _parse_markdown

if TYPE_CHECKING:
    from knowledge_forge.storage.store import PipelineStore

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


def _generated_at(frontmatter: dict[str, Any]) -> datetime | None:
    """`okf/writer.py` stamps every resources/*.md and objects/{type}/*.md
    file with `generated.at` (an ISO 8601 string - the source's
    `collected_at` for a resource, the KnowledgeObject's `extracted_at` for
    an object) - the one timestamp available on every file this module
    scans, so it doubles as the temporal-filter anchor (search/temporal.py)."""
    generated = frontmatter.get("generated")
    at = generated.get("at") if isinstance(generated, dict) else None
    if not at:
        return None
    try:
        return datetime.fromisoformat(at)
    except (TypeError, ValueError):
        return None


async def file_search(
    query: str,
    project_id: str | None = None,
    type: str | None = None,
    limit: int = 20,
    okf_root: str | Path | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict[str, Any]]:
    """OKF 파일 트리 위의 키워드/frontmatter 검색 - wiki/agent-search.md의 file_search 계약.

    `okf_root`를 생략하면 `KF_OKF_DIR`(settings.okf_dir)을 쓴다 - OKF 트리가 설정되어 있지 않거나
    존재하지 않으면 빈 리스트를 반환한다(에러가 아님 - 도구 호출 루프에서 "결과 없음"과 동일하게
    다뤄지면 되는 상황).

    `since`/`until`(둘 다 tz-aware, `until`은 배타적)이 주어지면 frontmatter의 `generated.at`
    (`_generated_at`)이 그 범위 밖인 파일은 제외한다 - `since`/`until`이 주어졌는데
    `generated.at`이 없거나 파싱 실패하면(손상된 프론트매터 등) 안전하게 제외한다(있는지 없는지
    모르는 걸 포함시키지 않음). `search/temporal.py::parse_temporal_expression`이 자연어
    시간 표현을 이 두 값으로 바꿔주는 쪽이고, 이 함수 자체는 자연어를 이해하지 않는다.
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
            if since is not None or until is not None:
                generated_at = _generated_at(frontmatter)
                if generated_at is None:
                    continue
                if since is not None and generated_at < since:
                    continue
                if until is not None and generated_at >= until:
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
                        # Only present for objects/{type}/*.md files (okf/writer.py
                        # always writes this for a KnowledgeObject-derived file) -
                        # None for resources/*.md (source-level, no KO). Lets a
                        # caller that also has vector_search hits (which are
                        # keyed by knowledge_object_id) recognize "this is the
                        # same KnowledgeObject the other source also found"
                        # (search/rerank.py's cross-source dedup).
                        "knowledge_object_id": frontmatter.get("knowledge_object_id"),
                    },
                )
            )

    hits.sort(key=lambda pair: (-pair[0], pair[1]["path"]))
    return [hit for _, hit in hits[:limit]]


async def vector_search(
    query: str,
    store: "PipelineStore",
    project_id: str | None = None,
    type: str | None = None,
    limit: int = 10,
    embedding_model: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict[str, Any]]:
    """`knowledge_object_embeddings`의 HNSW 인덱스를 실제로 읽는 첫 소비자
    (wiki/agent-search.md의 vector_search 계약) - `file_search`가 강한 정확한
    문자열 일치 대신, 자연어 패러프레이즈/개념 검색에 강하다.

    `query`를 요청 시점에 `get_embedding_provider(embedding_model)`로 임베딩한
    뒤 `store.search_by_embedding()`(HNSW `<=>` ORDER BY)으로 조회한다 -
    `file_search`와 달리 `store`를 요구하는 이유는 Postgres 연결 자체를 이
    함수가 대신 열어줄 수 없기 때문(호출자가 `PipelineStore`를 이미 연결해
    쥐고 있어야 함 - `agent/tools.py`의 `_make_vector_search_tool` 참고).

    `embedding_model`을 생략하면 `KF_EMBEDDING_MODEL`(현재 활성 모델)을 쓴다 -
    `knowledge_object_embeddings`의 PK가 `(knowledge_object_id,
    embedding_model)`이라 여러 모델의 벡터가 동시에 존재할 수 있으므로, 검색도
    어느 모델의 벡터 공간에서 비교할지 명시해야 한다(`embeddings.md` 참고).

    `since`/`until`(둘 다 tz-aware, `until`은 배타적)이 주어지면 `knowledge_objects.extracted_at`
    이 그 범위 안인 것만 반환한다 - `file_search`의 `since`/`until`과 같은 계약,
    `search/temporal.py::parse_temporal_expression`이 자연어를 이 두 값으로 바꿔준다.

    임베딩이 없는 KnowledgeObject(백필 전, 또는 `--no-embed`로 저장된 것)는
    결과에 그냥 나오지 않는다 - `knowledge_object_embeddings`에 조인하는
    쿼리라 애초에 후보에 없다(agent-search.md의 "아직 결정 안 된 것"에 대한
    1차 답: 에러도 경고도 없이 조용히 빠짐)."""
    from knowledge_forge.embedding.factory import get_embedding_provider

    query = query.strip()
    if not query:
        return []

    model = embedding_model or settings.embedding_model
    embedder = get_embedding_provider(model=model)
    [vector] = await embedder.embed([query])
    return await store.search_by_embedding(
        vector, model, project_id=project_id, type=type, limit=limit, since=since, until=until
    )
