"""Read-only counterpart to `writer.py`, for a file-based OKF viewer
(api/routes/okf.py) - never called by the extraction pipeline itself.

Reuses `writer._parse_markdown()`/`writer._FRONTMATTER_RE` so the read side
never drifts from the format `OKFWriter` actually produces.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path
from typing import Any

import yaml
from markdown_it import MarkdownIt

from knowledge_forge.okf.writer import _parse_markdown

_MANIFEST_FILE = "manifest.yaml"
_MD_LINK_RE = re.compile(r"(\]\()([^)\s]+\.md)(\))")

_md = MarkdownIt("gfm-like").disable("linkify")


def list_projects(okf_root: Path) -> list[str]:
    if not okf_root.is_dir():
        return []
    return sorted(p.name for p in okf_root.iterdir() if (p / _MANIFEST_FILE).is_file())


def read_manifest(project_root: Path) -> dict[str, Any]:
    text = (project_root / _MANIFEST_FILE).read_text(encoding="utf-8")
    return yaml.safe_load(text) or {}


def _resolve_rel_path(project_root: Path, rel_path: str) -> Path:
    """Rejects any `rel_path` that escapes `project_root` (e.g. `../../etc/passwd`,
    an absolute path) - `rel_path` comes straight from the URL."""
    candidate = (project_root / rel_path).resolve()
    root = project_root.resolve()
    if root not in candidate.parents and candidate != root:
        raise ValueError(f"rel_path escapes the project root: {rel_path}")
    return candidate


def _normalize_ref(ref: str, from_rel_path: str) -> str:
    """Resolves `ref` (a path relative to the file at `from_rel_path`, as found in
    a markdown link target or in a `sources`/`relations` frontmatter entry) to a
    project-root-relative path, so every consumer (the SPA's link handler, the
    graph builder) can key on the same id regardless of which file it was read
    from."""
    from_dir = posixpath.dirname(from_rel_path)
    return posixpath.normpath(posixpath.join(from_dir or ".", ref))


def _normalize_links(body: str, from_rel_path: str) -> str:
    """Rewrites every `[..](relative/path.md)` in `body` to a project-root-relative
    href (no leading `./`/`../`), so the frontend can treat any `*.md` href as an
    internal nav target without knowing the current file's directory depth."""

    def _rewrite(m: re.Match) -> str:
        target = m.group(2)
        if "://" in target:
            return m.group(0)
        return f"{m.group(1)}{_normalize_ref(target, from_rel_path)}{m.group(3)}"

    return _MD_LINK_RE.sub(_rewrite, body)


def read_markdown_file(project_root: Path, rel_path: str) -> dict[str, Any]:
    path = _resolve_rel_path(project_root, rel_path)
    if not path.is_file():
        raise FileNotFoundError(rel_path)
    frontmatter, body = _parse_markdown(path.read_text(encoding="utf-8"))
    body_html = _md.render(_normalize_links(body, rel_path))
    return {"rel_path": rel_path, "frontmatter": frontmatter, "body_html": body_html}


def _title_from_path(path: str) -> str:
    return posixpath.basename(path).removesuffix(".md")


def build_graph(project_root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Assembles a Cytoscape-ready graph straight from the frontmatter `OKFWriter`
    already denormalizes onto each concept file - no separate relations/ store to
    query. `sources`/`relations` entries are relative to *that* object's own path
    (like a markdown link target), so `_normalize_ref` is reused to key every node
    by the same project-root-relative path `list_objects_by_type()`/
    `read_markdown_file()` already use.

    Only `direction == "out"` relation entries become edges - `write_relations()`
    denormalizes each edge onto both endpoints (`direction: out` on the subject,
    `direction: in` on the object), so taking one side once is enough and avoids
    double-counting.
    """
    documents = []
    for entry in manifest.get("resources", []):
        path = entry["path"]
        frontmatter, _ = _parse_markdown((project_root / path).read_text(encoding="utf-8"))
        documents.append(
            {
                "id": path,
                "source_type": frontmatter.get("source_type") or entry.get("type"),
                "title": frontmatter.get("title") or _title_from_path(path),
                "description": frontmatter.get("resource"),
            }
        )

    objects = []
    edges = []
    for entry in manifest.get("objects", []):
        path = entry["path"]
        frontmatter, _ = _parse_markdown((project_root / path).read_text(encoding="utf-8"))
        sources = frontmatter.get("sources") or []
        parent = _normalize_ref(sources[0]["resource"], path) if sources else None
        objects.append(
            {
                "id": path,
                "type": entry["type"],
                "title": frontmatter.get("title") or _title_from_path(path),
                "parent": parent,
            }
        )
        for rel in frontmatter.get("relations") or []:
            if rel.get("direction") != "out":
                continue
            edges.append(
                {
                    "id": rel["relation_id"],
                    "source": path,
                    "target": _normalize_ref(rel["peer"], path),
                    "type": rel.get("type"),
                }
            )

    return {"documents": documents, "objects": objects, "edges": edges}


def list_objects_by_type(project_root: Path, manifest: dict[str, Any], type_: str) -> list[dict[str, Any]]:
    cards = []
    for entry in manifest.get("objects", []):
        if entry.get("type") != type_:
            continue
        frontmatter, _ = _parse_markdown((project_root / entry["path"]).read_text(encoding="utf-8"))
        cards.append(
            {
                "id": entry["id"],
                "path": entry["path"],
                "title": frontmatter.get("title"),
                "description": frontmatter.get("description"),
                "tags": frontmatter.get("tags") or [],
                "status": frontmatter.get("status"),
                "resource": frontmatter.get("resource"),
            }
        )
    return cards
