from datetime import datetime, timezone

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.storage.raw_store import RawBlobStore


def _item(content: bytes, content_hash: str) -> RawItem:
    return RawItem(
        uri="file:///a.txt",
        content=content,
        content_hash=content_hash,
        modified_at=datetime.now(timezone.utc),
        metadata={},
    )


def test_save_writes_content_addressed_blob(tmp_path):
    store = RawBlobStore(tmp_path)
    path = store.save(_item(b"hello", "abcd1234"))

    assert path.exists()
    assert path.read_bytes() == b"hello"
    assert path == tmp_path / "raw" / "ab" / "abcd1234"


def test_save_deduplicates_by_hash(tmp_path):
    store = RawBlobStore(tmp_path)
    store.save(_item(b"hello", "abcd1234"))
    path = store.save(_item(b"hello again but same hash", "abcd1234"))

    # Second save must not overwrite - content stays whatever was written first.
    assert path.read_bytes() == b"hello"
