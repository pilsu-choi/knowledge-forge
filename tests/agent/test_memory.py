from knowledge_forge.agent.memory import read_memory, write_memory
from knowledge_forge.config import settings


def _use_tmp_memory_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "agent_memory_dir", str(tmp_path))


def test_read_memory_returns_empty_string_when_nothing_written(monkeypatch, tmp_path):
    _use_tmp_memory_dir(monkeypatch, tmp_path)

    assert read_memory("proj-a") == ""


def test_write_memory_creates_the_file_and_is_readable_back(monkeypatch, tmp_path):
    _use_tmp_memory_dir(monkeypatch, tmp_path)

    write_memory("fact one", project_id="proj-a")

    assert read_memory("proj-a") == "fact one\n"
    assert (tmp_path / "proj-a.md").is_file()


def test_write_memory_append_mode_adds_without_removing_prior_content(monkeypatch, tmp_path):
    _use_tmp_memory_dir(monkeypatch, tmp_path)

    write_memory("fact one", project_id="proj-a")
    write_memory("fact two", project_id="proj-a", mode="append")

    assert read_memory("proj-a") == "fact one\nfact two\n"


def test_write_memory_replace_mode_discards_prior_content(monkeypatch, tmp_path):
    _use_tmp_memory_dir(monkeypatch, tmp_path)

    write_memory("fact one", project_id="proj-a")
    write_memory("fact two", project_id="proj-a", mode="replace")

    assert read_memory("proj-a") == "fact two\n"


def test_memory_is_scoped_per_project_id(monkeypatch, tmp_path):
    _use_tmp_memory_dir(monkeypatch, tmp_path)

    write_memory("a-only", project_id="proj-a")
    write_memory("b-only", project_id="proj-b")

    assert read_memory("proj-a") == "a-only\n"
    assert read_memory("proj-b") == "b-only\n"


def test_no_project_id_uses_a_global_memory_file(monkeypatch, tmp_path):
    _use_tmp_memory_dir(monkeypatch, tmp_path)

    write_memory("global fact")

    assert read_memory() == "global fact\n"
    assert (tmp_path / "_global.md").is_file()


def test_project_id_with_path_separators_is_sanitized(monkeypatch, tmp_path):
    _use_tmp_memory_dir(monkeypatch, tmp_path)

    write_memory("x", project_id="a/b")

    assert not (tmp_path / "a").exists()
    assert (tmp_path / "a_b.md").is_file()
