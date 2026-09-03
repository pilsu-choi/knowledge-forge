"""Filesystem read/write tools for the agent runtime (wiki/agent-runtime.md's
FileSystem 도구) - every `path` is relative to the OKF root and resolved
through `reader._resolve_rel_path`, the same escape guard `read_markdown_file`
already uses for its own `rel_path` argument, just anchored at the OKF root
instead of a single project root. That's deliberate: `path` is meant to be
the exact string `search.service.file_search` already returns in its hits
(`"<project_id>/resources/...`"`), so an agent can `file_search` then
`read_file`/`edit_file` the same path with no translation.

Plain functions, not a class, importing nothing from the agent harness -
same "thin adapter over a plain service function" layering as
`search/service.py` (agent/tools.py wraps these as `ToolSpec`s, never the
other way around). Marked `async def` for the same reason `file_search` is:
consistency with the rest of this layer, even though the bodies below are
synchronous local file I/O.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from knowledge_forge.config import settings
from knowledge_forge.okf.reader import _resolve_rel_path


def _root(okf_root: str | Path | None) -> Path:
    root = okf_root if okf_root is not None else settings.okf_dir
    if not root:
        raise ValueError("OKF 루트가 설정되어 있지 않습니다 (KF_OKF_DIR).")
    path = Path(root)
    if not path.is_dir():
        raise ValueError(f"OKF 루트 디렉터리가 존재하지 않습니다: {path}")
    return path


async def read_file(path: str, okf_root: str | Path | None = None) -> dict[str, Any]:
    root = _root(okf_root)
    target = _resolve_rel_path(root, path)
    if not target.is_file():
        raise FileNotFoundError(path)
    return {"path": path, "content": target.read_text(encoding="utf-8")}


async def write_file(path: str, content: str, okf_root: str | Path | None = None) -> dict[str, Any]:
    """Creates `path` (and any missing parent directories) or overwrites it
    if it already exists - full-content write, not a patch. `edit_file` is
    the surgical alternative for an existing file."""
    root = _root(okf_root)
    target = _resolve_rel_path(root, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"path": path, "bytes_written": len(content.encode("utf-8"))}


async def edit_file(path: str, old_text: str, new_text: str, okf_root: str | Path | None = None) -> dict[str, Any]:
    """Replaces exactly one occurrence of `old_text` with `new_text` in an
    existing file - mirrors the "old_text must be uniquely identifying"
    contract of Claude Code's own Edit tool, so a caller must widen
    `old_text` with more surrounding context rather than the tool guessing
    which occurrence was meant."""
    root = _root(okf_root)
    target = _resolve_rel_path(root, path)
    if not target.is_file():
        raise FileNotFoundError(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old_text)
    if count == 0:
        raise ValueError(f"old_text가 {path}에서 발견되지 않았습니다.")
    if count > 1:
        raise ValueError(f"old_text가 {path}에서 {count}번 일치합니다 - 유일하게 식별되도록 더 긴 문맥을 포함하세요.")
    target.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
    return {"path": path, "replaced": True}


async def list_dir(path: str = "", okf_root: str | Path | None = None) -> list[dict[str, Any]]:
    """Lists the immediate children of `path` (default: the OKF root itself,
    i.e. the project list) - not recursive, so the agent can walk the tree
    one level at a time the way it would with `ls`."""
    root = _root(okf_root)
    target = _resolve_rel_path(root, path)
    if not target.is_dir():
        raise NotADirectoryError(path or ".")
    return [
        {
            "name": child.name,
            "path": str(child.relative_to(root)),
            "type": "dir" if child.is_dir() else "file",
        }
        for child in sorted(target.iterdir())
    ]
