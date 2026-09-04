"""`ir_runner`의 코드 프로버넌스 - 진짜 임시 git 저장소를 만들어서 검증한다.

가짜로 `git status` 출력을 흉내 내면 정작 지키고 싶은 성질("서로 다른 작업 트리는
서로 다른 값으로 기록된다")을 git이 아니라 목이 보증하게 되므로, 여기서는
tmp_path에 실제 저장소를 만들고 실제로 파일을 고친다.

동기가 된 사고: 커밋 b470057 하나에 ablate-vector(ndcg@10 0.736)와
purity-vector-only(0.477)가 같은 git_commit으로 적혔고, 후자는 나중에 97c0c50으로
커밋된 수정된 작업 트리였다. 아래 test_two_different_dirty_trees_do_not_collide가
그 상황 자체다.
"""

import subprocess

import pytest

from knowledge_forge.evaluation.ir_runner import (
    DIRTY_MARKER,
    base_commit,
    current_code_provenance,
    current_git_commit,
    format_code_identity,
    is_dirty_commit,
)


def _git(cwd, *args: str) -> str:
    out = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """커밋 하나짜리 깨끗한 저장소로 chdir한 상태."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "first")
    monkeypatch.chdir(root)
    return root


def test_clean_tree_records_the_bare_commit(repo):
    head = _git(repo, "rev-parse", "HEAD")
    prov = current_code_provenance()
    assert prov.commit == head
    assert prov.diff_hash is None
    assert prov.dirty_files == []
    # 깨끗한 트리의 기록 값은 예전과 완전히 동일해야 한다 - 이 변경이 과거 행의
    # 표기를 바꿨다면 60개가 넘는 기존 기록과 비교가 안 된다.
    assert prov.identity == head
    assert is_dirty_commit(prov.identity) is False
    assert base_commit(prov.identity) == head


def test_dirty_tree_is_recorded_distinguishably(repo):
    head = _git(repo, "rev-parse", "HEAD")
    (repo / "mod.py").write_text("VALUE = 2\n", encoding="utf-8")

    prov = current_code_provenance()
    assert prov.commit == head
    assert prov.diff_hash is not None and len(prov.diff_hash) == 12
    assert prov.identity == f"{head}{DIRTY_MARKER}{prov.diff_hash}"
    # 커밋이 접두사로 남아 `LIKE 'sha%'` 조회가 그대로 산다.
    assert prov.identity.startswith(head)
    assert base_commit(prov.identity) == head
    assert is_dirty_commit(prov.identity) is True
    assert [line.strip() for line in prov.dirty_files] == ["M mod.py"]


def test_two_different_dirty_trees_do_not_collide(repo):
    """0.736과 0.477이 같은 값으로 적혔던 그 상황. 두 dirty 상태는 서로 다른
    식별자를 받아야 하고, 같은 상태는 같은 값으로 재현돼야 한다."""
    (repo / "mod.py").write_text("VALUE = 2\n", encoding="utf-8")
    first = current_code_provenance().identity
    assert current_code_provenance().identity == first  # 결정적

    (repo / "mod.py").write_text("VALUE = 3\n", encoding="utf-8")
    second = current_code_provenance().identity

    assert first != second
    assert base_commit(first) == base_commit(second)  # 같은 커밋인데도 구분된다


def test_staged_change_counts_as_dirty(repo):
    (repo / "mod.py").write_text("VALUE = 9\n", encoding="utf-8")
    _git(repo, "add", "mod.py")
    prov = current_code_provenance()
    # staged든 unstaged든 "HEAD가 아닌 코드"라는 점은 같다 - `git diff HEAD`가
    # 둘 다 본다.
    assert is_dirty_commit(prov.identity)


def test_untracked_file_content_moves_the_hash(repo):
    """`git diff HEAD`는 tracked 파일만 본다. 새로 만든 모듈 하나로 파이프라인이
    바뀌는 경우가 실제 worktree 작업의 기본값이라 내용까지 해시에 넣는다."""
    new = repo / "extra.py"
    new.write_text("HELPER = 1\n", encoding="utf-8")
    with_file = current_code_provenance().identity
    assert is_dirty_commit(with_file)

    new.write_text("HELPER = 2\n", encoding="utf-8")  # 이름은 그대로, 내용만 변경
    edited = current_code_provenance().identity
    assert edited != with_file


def test_gitignored_paths_do_not_make_a_run_dirty(repo):
    """.gitignore에 걸린 산출물(out/, .venv 등)은 코드가 아니다. 여기에 반응하면
    거의 모든 run이 dirty로 찍혀 표식이 무의미해진다."""
    ignored = repo / "ignored"
    ignored.mkdir()
    (ignored / "big.bin").write_bytes(b"x" * 1024)
    prov = current_code_provenance()
    assert prov.diff_hash is None
    assert prov.identity == _git(repo, "rev-parse", "HEAD")


def test_not_a_git_repo_returns_none_and_does_not_crash(tmp_path, monkeypatch):
    """tarball 체크아웃(=git 저장소가 아님)에서도 eval 하네스는 그냥 돌아야 한다."""
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    if current_git_commit() is not None:  # pragma: no cover - tmp_path가 저장소 안일 때
        pytest.skip("tmp_path is inside a git repository")
    prov = current_code_provenance()
    assert prov.commit is None
    assert prov.identity is None
    assert prov.dirty_files == []


def test_git_binary_missing_is_swallowed(repo, monkeypatch):
    def _boom(*args, **kwargs):
        raise OSError("git not found")

    monkeypatch.setattr(subprocess, "run", _boom)
    prov = current_code_provenance()
    assert prov.commit is None
    assert prov.identity is None


def test_identity_helpers_on_recorded_values():
    sha = "b470057d93c004101fc983be26d8c06e050e47de"
    assert base_commit(sha) == sha
    assert base_commit(f"{sha}{DIRTY_MARKER}0123456789ab") == sha
    assert base_commit(None) is None
    assert is_dirty_commit(None) is False
    assert format_code_identity(None) == "-"
    assert format_code_identity(sha) == "b470057"
    assert format_code_identity(f"{sha}{DIRTY_MARKER}0123456789ab") == "b470057+dirty"
