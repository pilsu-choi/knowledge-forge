from pathlib import Path

from knowledge_forge.collectors.local_file import LocalFileCollector


def test_initial_sync_yields_all_files(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("world")

    state_path = tmp_path / "state.json"
    collector = LocalFileCollector(root=tmp_path, state_path=state_path)
    items = list(collector.initial_sync())

    assert len(items) == 2
    uris = {item.uri for item in items}
    assert uris == {f"file://{tmp_path / 'a.txt'}", f"file://{tmp_path / 'sub' / 'b.txt'}"}
    assert all(not item.deleted for item in items)
    assert state_path.exists()


def test_incremental_sync_detects_new_modified_deleted(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "b.txt").write_text("world")
    state_path = tmp_path / "state.json"

    collector = LocalFileCollector(root=tmp_path, state_path=state_path)
    list(collector.initial_sync())
    cursor = collector.get_cursor()

    # unchanged: a.txt
    # modified: b.txt
    (tmp_path / "b.txt").write_text("world, changed")
    # new: c.txt
    (tmp_path / "c.txt").write_text("new file")
    # deleted: none yet, but we'll delete a.txt
    (tmp_path / "a.txt").unlink()

    collector2 = LocalFileCollector(root=tmp_path, state_path=state_path)
    items = list(collector2.incremental_sync(cursor))

    by_uri = {item.uri: item for item in items}
    assert len(items) == 3

    modified_uri = f"file://{tmp_path / 'b.txt'}"
    new_uri = f"file://{tmp_path / 'c.txt'}"
    deleted_uri = f"file://{tmp_path / 'a.txt'}"

    assert modified_uri in by_uri and not by_uri[modified_uri].deleted
    assert new_uri in by_uri and not by_uri[new_uri].deleted
    assert deleted_uri in by_uri and by_uri[deleted_uri].deleted


def test_incremental_sync_no_changes_yields_nothing(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello")
    state_path = tmp_path / "state.json"

    collector = LocalFileCollector(root=tmp_path, state_path=state_path)
    list(collector.initial_sync())
    cursor = collector.get_cursor()

    collector2 = LocalFileCollector(root=tmp_path, state_path=state_path)
    items = list(collector2.incremental_sync(cursor))

    assert items == []


def test_single_file_root(tmp_path: Path):
    file_path = tmp_path / "only.txt"
    file_path.write_text("solo file")

    collector = LocalFileCollector(root=file_path, state_path=None)
    items = list(collector.initial_sync())

    assert len(items) == 1
    assert items[0].uri == f"file://{file_path}"
    assert items[0].content == b"solo file"


def test_no_state_path_does_not_persist(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello")

    collector = LocalFileCollector(root=tmp_path, state_path=None)
    items = list(collector.initial_sync())
    assert len(items) == 1

    cursor = collector.get_cursor()
    assert cursor.data["manifest"]

    # No files under tmp_path should be created for state.
    assert not (tmp_path / ".knowledge-forge-state").exists()
