from datetime import datetime, timezone

import yaml

from knowledge_forge.models.chunk import HierarchyLevel
from knowledge_forge.models.document import Document, DocumentMetadata, TextBlock
from knowledge_forge.models.knowledge_object import DocumentRef, Evidence, KnowledgeObject, KnowledgeObjectType
from knowledge_forge.models.link import Link, LinkEndpoint, LinkType
from knowledge_forge.models.relation import Relation, RelationType
from knowledge_forge.models.source import Source, SourceRef, SourceType
from knowledge_forge.okf import OKFWriter
from knowledge_forge.okf.writer import _parse_markdown


def _sample_source_and_document():
    src = Source(
        source_id="s1",
        source_type=SourceType.LOCAL_FILE,
        uri="file:///a.md",
        content_hash="h1",
        collected_at=datetime.now(timezone.utc),
    )
    doc = Document(
        document_id="d1",
        source=SourceRef.from_source(src),
        elements=[TextBlock(element_id="e1", text="hello")],
        metadata=DocumentMetadata(title="T"),
    )
    return src, doc


def _sample_object(ko_id: str, ko_type: KnowledgeObjectType, title: str = "s") -> KnowledgeObject:
    return KnowledgeObject(
        knowledge_object_id=ko_id,
        type=ko_type,
        source=SourceRef(source_id="s1", source_type=SourceType.LOCAL_FILE, uri="file:///a.md"),
        document=DocumentRef(document_id="d1", title="T"),
        evidence=[Evidence(chunk_id="c1", document_id="d1", hierarchy_level=HierarchyLevel.PARAGRAPH, sequence_index=0)],
        summary="s",
        content={"statement": title} if ko_type != KnowledgeObjectType.ENTITY else {"name": title, "entity_type": "조직"},
        topics=["t"],
        document_type="report",
        extracted_at=datetime.now(timezone.utc),
        model_used="claude-opus-5",
        pipeline_version="0.1.0",
        source_hash="h1",
    )


def _find(root, subdir, glob="*.md"):
    matches = [p for p in (root / subdir).rglob(glob) if p.name != "index.md"]
    assert len(matches) == 1, f"expected exactly one match under {subdir}, got {matches}"
    return matches[0]


def test_writer_produces_conformant_markdown_concepts(tmp_path):
    writer = OKFWriter(tmp_path)
    src, doc = _sample_source_and_document()
    writer.write_resource(src, doc)

    fact = _sample_object("ko1", KnowledgeObjectType.FACT, title="fact statement")
    entity = _sample_object("ko2", KnowledgeObjectType.ENTITY, title="농협은행")
    writer.write_objects([fact, entity])

    root = tmp_path / "default"
    resource_path = _find(root, "resources")
    fact_path = _find(root, "objects/fact")
    entity_path = _find(root, "objects/entity")

    resource_fm, _ = _parse_markdown(resource_path.read_text(encoding="utf-8"))
    assert resource_fm["type"] == "Local File Source"
    assert resource_fm["source_id"] == "s1"

    fact_fm, fact_body = _parse_markdown(fact_path.read_text(encoding="utf-8"))
    assert fact_fm["type"] == "Fact"
    assert fact_fm["knowledge_object_id"] == "ko1"
    assert fact_fm["title"] == "fact statement"
    assert fact_fm["tags"] == ["t"]
    assert fact_fm["sources"][0]["resource"] == f"../../resources/{resource_path.name}"
    assert "fact statement" in fact_body

    entity_fm, entity_body = _parse_markdown(entity_path.read_text(encoding="utf-8"))
    assert entity_fm["type"] == "Entity"
    assert entity_fm["title"] == "농협은행"
    assert "유형" in entity_body


def test_write_relations_inlines_edge_on_both_endpoint_concepts(tmp_path):
    writer = OKFWriter(tmp_path)
    src, doc = _sample_source_and_document()
    writer.write_resource(src, doc)

    fact = _sample_object("ko1", KnowledgeObjectType.FACT, title="fact statement")
    entity = _sample_object("ko2", KnowledgeObjectType.ENTITY, title="농협은행")
    writer.write_objects([fact, entity])

    relation = Relation(
        relation_id="rel1",
        type=RelationType.MENTIONS,
        subject_id="ko1",
        object_id="ko2",
        evidence=["c1"],
        created_at=datetime.now(timezone.utc),
    )
    writer.write_relations([relation])

    root = tmp_path / "default"
    fact_path = _find(root, "objects/fact")
    entity_path = _find(root, "objects/entity")
    fact_fm, fact_body = _parse_markdown(fact_path.read_text(encoding="utf-8"))
    entity_fm, entity_body = _parse_markdown(entity_path.read_text(encoding="utf-8"))

    # A real relative link (../entity/x.md), not a leading-slash
    # "bundle-relative absolute" one - a plain file browser/IDE has no
    # notion of the project subtree as a root, so only a genuine relative
    # path resolves when the link is actually followed.
    assert fact_fm["relations"] == [
        {
            "relation_id": "rel1",
            "type": "mentions",
            "direction": "out",
            "peer": f"../entity/{entity_path.name}",
            "peer_title": "농협은행",
            "label": None,
            "confidence": None,
        }
    ]
    assert "## Relations" in fact_body
    assert f"mentions → [농협은행](../entity/{entity_path.name})" in fact_body

    assert entity_fm["relations"][0]["direction"] == "in"
    assert entity_fm["relations"][0]["peer_title"] == "fact statement"
    assert entity_fm["relations"][0]["peer"] == f"../fact/{fact_path.name}"
    assert f"mentions ← [fact statement](../fact/{fact_path.name})" in entity_body

    # No separate relation record was created - the edge was fully inlined.
    assert list((root / "relations").glob("*.md")) == []

    # Re-running the same relation must not duplicate the section (idempotent).
    writer.write_relations([relation])
    fact_fm2, fact_body2 = _parse_markdown(_find(root, "objects/fact").read_text(encoding="utf-8"))
    assert len(fact_fm2["relations"]) == 1
    assert fact_body2.count("## Relations") == 1


def test_write_relations_falls_back_to_orphan_file_when_endpoints_unresolved(tmp_path):
    writer = OKFWriter(tmp_path)
    relation = Relation(
        relation_id="rel-a",
        type=RelationType.MENTIONS,
        subject_id="ko-a",
        object_id="ko-a2",
        evidence=["c1"],
        created_at=datetime.now(timezone.utc),
    )
    writer.write_relations([relation], "PRJ-A")

    orphan = tmp_path / "PRJ-A" / "relations" / "rel-a.md"
    assert orphan.exists()
    fm, _ = _parse_markdown(orphan.read_text(encoding="utf-8"))
    assert fm["type"] == "Relation"
    assert fm["subject_id"] == "ko-a"
    assert not (tmp_path / "default" / "relations" / "rel-a.md").exists()


def test_write_links_inlines_edge_and_falls_back_when_unresolved(tmp_path):
    writer = OKFWriter(tmp_path)
    src, doc = _sample_source_and_document()
    writer.write_resource(src, doc)

    link = Link(
        link_id="lnk1",
        type=LinkType.THREAD_REPLY,
        source_ref=LinkEndpoint(source_id="s1", document_id="d1"),
        target_ref=LinkEndpoint(source_id="s1", document_id="d1"),
        created_at=datetime.now(timezone.utc),
    )
    writer.write_links([link])

    root = tmp_path / "default"
    resource_fm, resource_body = _parse_markdown(_find(root, "resources").read_text(encoding="utf-8"))
    assert {e["direction"] for e in resource_fm["links"]} == {"out", "in"}
    assert "## Links" in resource_body

    unresolved = Link(
        link_id="lnk-a",
        type=LinkType.SAME_CHANNEL_SEQUENCE,
        source_ref=LinkEndpoint(source_id="sa", document_id="da"),
        target_ref=LinkEndpoint(source_id="sa2", document_id="da2"),
        created_at=datetime.now(timezone.utc),
    )
    writer.write_links([unresolved], "PRJ-A")
    orphan = tmp_path / "PRJ-A" / "links" / "lnk-a.md"
    assert orphan.exists()
    assert not (tmp_path / "default" / "links" / "lnk-a.md").exists()


def test_writer_produces_manifest_indexing_resources_and_objects(tmp_path):
    writer = OKFWriter(tmp_path)
    src, doc = _sample_source_and_document()
    writer.write_resource(src, doc)

    fact = _sample_object("ko1", KnowledgeObjectType.FACT)
    entity = _sample_object("ko2", KnowledgeObjectType.ENTITY, title="농협은행")
    writer.write_objects([fact, entity])

    manifest_path = writer.write_manifest()["default"]
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))

    assert [r["id"] for r in manifest["resources"]] == ["s1"]
    assert manifest["resources"][0]["path"].endswith(".md")
    assert {o["id"] for o in manifest["objects"]} == {"ko1", "ko2"}
    fact_entry = next(o for o in manifest["objects"] if o["id"] == "ko1")
    assert fact_entry["type"] == "fact"
    assert fact_entry["path"].startswith("objects/fact/") and fact_entry["path"].endswith(".md")
    assert manifest["relations"] == []
    assert manifest["links"] == []
    assert manifest["pipeline_version"]
    assert manifest["generated_at"]


def test_manifest_reflects_objects_written_across_multiple_writer_instances(tmp_path):
    OKFWriter(tmp_path).write_objects([_sample_object("ko1", KnowledgeObjectType.FACT)])
    second_writer = OKFWriter(tmp_path)
    second_writer.write_objects([_sample_object("ko2", KnowledgeObjectType.EVENT)])
    manifest_path = second_writer.write_manifest()["default"]

    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert {o["id"] for o in manifest["objects"]} == {"ko1", "ko2"}


def test_write_views_builds_by_topic_log_and_indexes(tmp_path):
    writer = OKFWriter(tmp_path)
    src, doc = _sample_source_and_document()
    writer.write_resource(src, doc)

    fact = _sample_object("ko1", KnowledgeObjectType.FACT, title="fact statement")
    fact = fact.model_copy(update={"topics": ["alpha", "beta"], "extracted_at": datetime(2026, 1, 2, tzinfo=timezone.utc)})
    entity = _sample_object("ko2", KnowledgeObjectType.ENTITY, title="농협은행")
    entity = entity.model_copy(update={"topics": ["alpha"], "extracted_at": datetime(2026, 1, 1, tzinfo=timezone.utc)})
    writer.write_objects([fact, entity])

    paths = writer.write_views()["default"]
    names = {p.name for p in paths}
    assert "index.md" in names

    root = tmp_path / "default"
    by_topic = (root / "views" / "by-topic.md").read_text(encoding="utf-8")
    assert "## alpha" in by_topic and "## beta" in by_topic
    assert "fact statement" in by_topic and "농협은행" in by_topic

    log = (root / "log.md").read_text(encoding="utf-8")
    assert "## 2026-01-01" in log and "## 2026-01-02" in log
    assert log.index("2026-01-02") < log.index("2026-01-01")  # newest first

    bundle_index = (root / "index.md").read_text(encoding="utf-8")
    fm, _ = _parse_markdown(bundle_index)
    assert fm["okf_version"] == "0.2"
    assert "objects/fact/index.md" in bundle_index
    assert "objects/entity/index.md" in bundle_index

    fact_type_index = (root / "objects" / "fact" / "index.md").read_text(encoding="utf-8")
    assert "fact statement" in fact_type_index

    resource_body = (_find(root, "resources")).read_text(encoding="utf-8")
    assert "fact statement" in resource_body and "농협은행" in resource_body


def test_writer_routes_each_item_to_its_own_project_subtree(tmp_path):
    src_a = Source(
        source_id="sa",
        source_type=SourceType.SLACK,
        uri="slack://ca/1",
        content_hash="ha",
        collected_at=datetime.now(timezone.utc),
        project_id="PRJ-A",
    )
    doc_a = Document(
        document_id="da",
        source=SourceRef.from_source(src_a),
        elements=[TextBlock(element_id="ea", text="a")],
        metadata=DocumentMetadata(title="A"),
    )
    src_b = Source(
        source_id="sb",
        source_type=SourceType.SLACK,
        uri="slack://cb/1",
        content_hash="hb",
        collected_at=datetime.now(timezone.utc),
        project_id="PRJ-B",
    )
    doc_b = Document(
        document_id="db",
        source=SourceRef.from_source(src_b),
        elements=[TextBlock(element_id="eb", text="b")],
        metadata=DocumentMetadata(title="B"),
    )

    writer = OKFWriter(tmp_path)  # default project_id, never used here
    writer.write_resource(src_a, doc_a)
    writer.write_resource(src_b, doc_b)

    ko_a = _sample_object("ko-a", KnowledgeObjectType.FACT).model_copy(update={"source": SourceRef.from_source(src_a)})
    ko_b = _sample_object("ko-b", KnowledgeObjectType.FACT).model_copy(update={"source": SourceRef.from_source(src_b)})
    writer.write_objects([ko_a, ko_b])

    assert _find(tmp_path / "PRJ-A", "resources").exists()
    assert _find(tmp_path / "PRJ-B", "resources").exists()
    assert _find(tmp_path / "PRJ-A", "objects/fact").exists()
    assert _find(tmp_path / "PRJ-B", "objects/fact").exists()

    manifests = writer.write_manifest()
    assert set(manifests) == {"default", "PRJ-A", "PRJ-B"}
    manifest_a = yaml.safe_load(manifests["PRJ-A"].read_text(encoding="utf-8"))
    assert {o["id"] for o in manifest_a["objects"]} == {"ko-a"}
    manifest_b = yaml.safe_load(manifests["PRJ-B"].read_text(encoding="utf-8"))
    assert {o["id"] for o in manifest_b["objects"]} == {"ko-b"}


def test_supersedes_deprecates_the_prior_object(tmp_path):
    writer = OKFWriter(tmp_path)
    old = _sample_object("ko1", KnowledgeObjectType.FACT, title="old statement")
    writer.write_objects([old])

    new = _sample_object("ko1v2", KnowledgeObjectType.FACT, title="new statement").model_copy(
        update={"supersedes": "ko1", "version": 2}
    )
    writer.write_objects([new])

    root = tmp_path / "default"
    old_path = next(p for p in (root / "objects" / "fact").glob("*.md") if p.name != "index.md" and "old" in p.name)
    fm, body = _parse_markdown(old_path.read_text(encoding="utf-8"))
    assert fm["status"] == "deprecated"
    assert "Deprecated" in body
    assert "new statement" in body
