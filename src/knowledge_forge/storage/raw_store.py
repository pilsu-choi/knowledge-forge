"""Content-addressed disk storage for raw collected bytes.

Applies uniformly to any Collector, not just files already on local disk:
File sources merely happen to already live at `item.uri` (their `RawItem`
copy is a snapshot), while a future SaaS source (a Slack message, a Gmail
thread) has no durable local copy at all until this store writes one - the
snapshot also decouples the pipeline's record from a mutable/deletable
live source.
"""

from pathlib import Path

from knowledge_forge.collectors.base import RawItem


class RawBlobStore:
    def __init__(self, data_dir: Path | str):
        self._root = Path(data_dir) / "raw"

    def save(self, item: RawItem, project_id: str = "default") -> Path:
        """Write `item.content` under `{project_id}/{hash fan-out}`, deduplicating
        by content hash within that project. Returns the path even if the blob
        already existed. Blobs are scoped per project_id (not shared across
        projects) so a project's raw data can be located, backed up, or deleted
        as a unit.
        """
        fan_out = item.content_hash[:2]
        directory = self._root / project_id / fan_out
        path = directory / item.content_hash
        if not path.exists():
            directory.mkdir(parents=True, exist_ok=True)
            path.write_bytes(item.content)
        return path
