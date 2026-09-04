import textwrap
from pathlib import Path

from knowledge_forge.search.service import file_search


def _write(path: Path, frontmatter_yaml: str, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter_yaml.strip()}\n---\n\n{body.strip()}\n", encoding="utf-8")


def _seed_project(root: Path, project_id: str = "default") -> Path:
    project_root = root / project_id
    (project_root / "manifest.yaml").parent.mkdir(parents=True, exist_ok=True)
    (project_root / "manifest.yaml").write_text(f"project_id: {project_id}\n", encoding="utf-8")

    _write(
        project_root / "objects" / "decision" / "auth-decision-abc123.md",
        textwrap.dedent(
            """\
            type: Decision
            title: 인증 방식을 OAuth로 변경
            description: 기존 세션 기반 인증을 OAuth2로 교체하기로 결정
            tags: [auth, security]
            knowledge_object_id: ko-abc123
            """
        ),
        "## 본문\n레거시 세션 인증은 토큰 탈취 위험이 있어 OAuth2로 전환한다.",
    )
    _write(
        project_root / "resources" / "doc-xyz.md",
        textwrap.dedent(
            """\
            type: File
            title: 아키텍처 문서
            source_id: src-xyz
            """
        ),
        "## 추출된 개념\n(아직 없음)",
    )
    return project_root


async def test_matches_object_body_and_includes_frontmatter_fields(tmp_path):
    _seed_project(tmp_path)

    results = await file_search("OAuth", okf_root=tmp_path)

    assert len(results) == 1
    hit = results[0]
    assert hit["path"] == "default/objects/decision/auth-decision-abc123.md"
    assert hit["title"] == "인증 방식을 OAuth로 변경"
    assert hit["type"] == "decision"
    assert hit["tags"] == ["auth", "security"]
    assert hit["project_id"] == "default"
    assert "OAuth2" in hit["snippet"]
    assert hit["knowledge_object_id"] == "ko-abc123"


async def test_knowledge_object_id_is_none_for_resource_hits(tmp_path):
    _seed_project(tmp_path)

    results = await file_search("아키텍처", okf_root=tmp_path)

    assert len(results) == 1
    assert results[0]["knowledge_object_id"] is None


async def test_type_filter_excludes_resources_and_other_types(tmp_path):
    _seed_project(tmp_path)

    results = await file_search("OAuth", type="decision", okf_root=tmp_path)
    assert len(results) == 1

    no_match = await file_search("아키텍처", type="decision", okf_root=tmp_path)
    assert no_match == []


async def test_project_id_scopes_the_search(tmp_path):
    _seed_project(tmp_path, project_id="proj-a")
    _seed_project(tmp_path, project_id="proj-b")

    results = await file_search("OAuth", project_id="proj-a", okf_root=tmp_path)

    assert len(results) == 1
    assert results[0]["project_id"] == "proj-a"


async def test_no_match_returns_empty_list(tmp_path):
    _seed_project(tmp_path)

    assert await file_search("nonexistent-term", okf_root=tmp_path) == []


async def test_empty_query_returns_empty_list(tmp_path):
    _seed_project(tmp_path)

    assert await file_search("   ", okf_root=tmp_path) == []


async def test_missing_okf_root_returns_empty_list(tmp_path):
    assert await file_search("OAuth", okf_root=tmp_path / "does-not-exist") == []


async def test_title_match_ranks_above_body_only_match(tmp_path):
    project_root = _seed_project(tmp_path)
    # A second object that only matches in the body, not the title/description/tags.
    _write(
        project_root / "objects" / "entity" / "other-entity-def456.md",
        textwrap.dedent(
            """\
            type: Entity
            title: 다른 개체
            description: 관련 없는 설명
            knowledge_object_id: ko-def456
            """
        ),
        "본문 어딘가에 OAuth라는 단어가 언급된다.",
    )

    results = await file_search("OAuth", okf_root=tmp_path)

    assert [hit["title"] for hit in results] == ["인증 방식을 OAuth로 변경", "다른 개체"]
