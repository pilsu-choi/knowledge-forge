"""Cross-session agent memory (wiki/agent-runtime.md's Memory 도구) - one
plain markdown file per `project_id` (a `"_global"` file when none is given),
under `settings.agent_memory_dir`, deliberately OUTSIDE the OKF tree so
agent-authored notes never get mixed into the knowledge base itself or
picked up by `file_search`/`OKFWriter`.

Plain functions, not a class - same "thin service, thin ToolSpec adapter in
agent/tools.py" layering as `search/service.py`/`okf/fs_service.py`. Unlike
those, this one is synchronous: it has no path-escape concern to share (the
path is always exactly `agent_memory_dir/<project_id>.md`, built here, never
taken from model input) and no async caller needs it to be a coroutine -
`agent/tools.py` wraps it in an `async def` handler like everything else.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from knowledge_forge.config import settings

_GLOBAL_KEY = "_global"


def _memory_path(project_id: str | None) -> Path:
    key = project_id or _GLOBAL_KEY
    # project_id is developer-controlled (CLI --project-id / API), not raw
    # model input, but still guard the filename against a stray separator.
    safe_key = key.replace("/", "_").replace("\\", "_")
    return Path(settings.agent_memory_dir) / f"{safe_key}.md"


def read_memory(project_id: str | None = None) -> str:
    """Returns the current memory content, or "" if nothing has been written
    yet - never an error, same "empty means nothing to report" convention as
    `file_search` on a missing OKF root."""
    path = _memory_path(project_id)
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def write_memory(content: str, project_id: str | None = None, mode: Literal["append", "replace"] = "append") -> str:
    """`mode="append"` (default) adds `content` as a new entry without
    touching what's already there - the safe default, since memory is meant
    to accumulate across sessions and a careless full overwrite would lose
    prior notes. `mode="replace"` is the explicit escape hatch for when the
    agent (or a user) decides the file itself needs cleaning up. Returns the
    resulting full content so a caller can confirm what memory now holds."""
    path = _memory_path(project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = content.strip() + "\n"
    if mode == "append" and path.is_file():
        with path.open("a", encoding="utf-8") as f:
            f.write(entry)
    else:
        path.write_text(entry, encoding="utf-8")
    return read_memory(project_id)
