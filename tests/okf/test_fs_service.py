import pytest

from knowledge_forge.okf.fs_service import edit_file, list_dir, read_file, write_file


async def test_read_file_returns_content(tmp_path):
    (tmp_path / "default").mkdir()
    (tmp_path / "default" / "note.md").write_text("hello", encoding="utf-8")

    result = await read_file("default/note.md", okf_root=tmp_path)

    assert result == {"path": "default/note.md", "content": "hello"}


async def test_read_file_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        await read_file("default/missing.md", okf_root=tmp_path)


async def test_read_file_rejects_path_escaping_root(tmp_path):
    with pytest.raises(ValueError, match="escapes"):
        await read_file("../outside.md", okf_root=tmp_path)


async def test_write_file_creates_missing_parent_dirs(tmp_path):
    result = await write_file("default/notes/new.md", "content", okf_root=tmp_path)

    assert (tmp_path / "default" / "notes" / "new.md").read_text(encoding="utf-8") == "content"
    assert result == {"path": "default/notes/new.md", "bytes_written": len("content")}


async def test_write_file_overwrites_existing_file(tmp_path):
    (tmp_path / "default").mkdir()
    (tmp_path / "default" / "note.md").write_text("old", encoding="utf-8")

    await write_file("default/note.md", "new", okf_root=tmp_path)

    assert (tmp_path / "default" / "note.md").read_text(encoding="utf-8") == "new"


async def test_edit_file_replaces_the_unique_match(tmp_path):
    (tmp_path / "default").mkdir()
    (tmp_path / "default" / "note.md").write_text("before\nmiddle\nafter", encoding="utf-8")

    result = await edit_file("default/note.md", "middle", "changed", okf_root=tmp_path)

    assert (tmp_path / "default" / "note.md").read_text(encoding="utf-8") == "before\nchanged\nafter"
    assert result == {"path": "default/note.md", "replaced": True}


async def test_edit_file_rejects_zero_matches(tmp_path):
    (tmp_path / "default").mkdir()
    (tmp_path / "default" / "note.md").write_text("content", encoding="utf-8")

    with pytest.raises(ValueError, match="발견되지 않았습니다"):
        await edit_file("default/note.md", "missing", "x", okf_root=tmp_path)


async def test_edit_file_rejects_ambiguous_matches(tmp_path):
    (tmp_path / "default").mkdir()
    (tmp_path / "default" / "note.md").write_text("dup dup", encoding="utf-8")

    with pytest.raises(ValueError, match="2번 일치"):
        await edit_file("default/note.md", "dup", "x", okf_root=tmp_path)


async def test_list_dir_lists_okf_root_by_default(tmp_path):
    (tmp_path / "default").mkdir()
    (tmp_path / "other").mkdir()

    entries = await list_dir(okf_root=tmp_path)

    assert entries == [
        {"name": "default", "path": "default", "type": "dir"},
        {"name": "other", "path": "other", "type": "dir"},
    ]


async def test_list_dir_lists_a_subdirectory(tmp_path):
    (tmp_path / "default" / "resources").mkdir(parents=True)
    (tmp_path / "default" / "resources" / "a.md").write_text("x", encoding="utf-8")

    entries = await list_dir("default/resources", okf_root=tmp_path)

    assert entries == [{"name": "a.md", "path": "default/resources/a.md", "type": "file"}]
