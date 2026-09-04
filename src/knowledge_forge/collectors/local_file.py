"""Filesystem Collector - the working vertical slice for the File source type.

Cursor shape: `Cursor.data == {"manifest": {"<absolute path>": {"hash": str, "mtime": float}}}`.
`get_cursor()` and `incremental_sync()` agree on this shape; `state_path`
(when given) is just where that manifest is persisted between runs.
"""

from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from knowledge_forge.collectors.base import Collector, Cursor, RawItem
from knowledge_forge.ids import content_hash as compute_content_hash
from knowledge_forge.models.source import SourceType


def _default_state_path(root: Path) -> Path:
    from knowledge_forge.ids import stable_id

    return Path(".knowledge-forge-state") / f"{stable_id(str(root))[:16]}.json"


class LocalFileCollector(Collector):
    source_type = SourceType.LOCAL_FILE

    def __init__(self, root: Path | str, state_path: Path | str | None = None):
        self._root = Path(root).resolve()
        # `state_path=None` disables persistence: initial_sync() always behaves
        # as if there is no prior state, and get_cursor()/incremental_sync()
        # operate on an in-memory manifest built during this collector's
        # lifetime rather than reading/writing a file.
        self._state_path = Path(state_path).resolve() if state_path is not None else None
        self._manifest: dict[str, dict] = {}
        if self._state_path is not None:
            self._manifest = self._read_manifest(self._state_path)

    @staticmethod
    def _read_manifest(state_path: Path) -> dict[str, dict]:
        if not state_path.exists():
            return {}
        import json

        return json.loads(state_path.read_text(encoding="utf-8"))

    def _write_manifest(self) -> None:
        if self._state_path is None:
            return
        import json

        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(self._manifest), encoding="utf-8")

    def _walk_files(self) -> list[Path]:
        if self._root.is_file():
            return [self._root]
        return sorted(
            p
            for p in self._root.rglob("*")
            if p.is_file() and (self._state_path is None or p.resolve() != self._state_path)
        )

    @staticmethod
    def _modified_at(path: Path) -> datetime:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)

    def _make_item(self, path: Path) -> RawItem:
        data = path.read_bytes()
        # `file://` + absolute path preserves the original location per the
        # PRD's "원본 위치/URL 보존" requirement, independent of manifest state.
        return RawItem(
            uri=f"file://{path}",
            content=data,
            content_hash=compute_content_hash(data),
            modified_at=self._modified_at(path),
            metadata={"filename": path.name, "size_bytes": len(data)},
            deleted=False,
        )

    def initial_sync(self) -> Iterator[RawItem]:
        manifest: dict[str, dict] = {}
        for path in self._walk_files():
            item = self._make_item(path)
            manifest[str(path)] = {"hash": item.content_hash, "mtime": path.stat().st_mtime}
            yield item
        self._manifest = manifest
        self._write_manifest()

    def incremental_sync(self, cursor: Cursor) -> Iterator[RawItem]:
        prior_manifest: dict[str, dict] = cursor.data.get("manifest", {})
        current_paths = self._walk_files()
        current_path_strs = {str(p) for p in current_paths}
        new_manifest: dict[str, dict] = {}

        for path in current_paths:
            path_str = str(path)
            data = path.read_bytes()
            h = compute_content_hash(data)
            new_manifest[path_str] = {"hash": h, "mtime": path.stat().st_mtime}
            prior_entry = prior_manifest.get(path_str)
            if prior_entry is None or prior_entry.get("hash") != h:
                yield RawItem(
                    uri=f"file://{path}",
                    content=data,
                    content_hash=h,
                    modified_at=self._modified_at(path),
                    metadata={"filename": path.name, "size_bytes": len(data)},
                    deleted=False,
                )

        for path_str in prior_manifest:
            if path_str not in current_path_strs:
                yield RawItem(
                    uri=f"file://{path_str}",
                    content=b"",
                    content_hash="",
                    modified_at=datetime.now(tz=timezone.utc),
                    metadata={"filename": Path(path_str).name},
                    deleted=True,
                )

        self._manifest = new_manifest
        self._write_manifest()

    def get_cursor(self) -> Cursor:
        manifest = self._manifest
        if self._state_path is not None:
            manifest = self._read_manifest(self._state_path)
        return Cursor(data={"manifest": manifest})
