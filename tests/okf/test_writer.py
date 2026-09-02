from datetime import datetime, timezone

import yaml

from knowledge_forge.models.chunk import HierarchyLevel
from knowledge_forge.models.document import Document, DocumentMetadata, TextBlock
from knowledge_forge.models.knowledge_object import DocumentRef, Evidence, KnowledgeObject, KnowledgeObjectType
from knowledge_forge.models.relation import Relation, RelationType
from knowledge_forge.models.source import Source, SourceRef, SourceType
from knowledge_forge.okf import OKFWriter


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


def _sample_object(ko_id: str, ko_type: KnowledgeObjectType) -> KnowledgeObject:
    return KnowledgeObject(
        knowledge_object_id=ko_id,
        type=ko_type,
        source=SourceRef(source_id="s1", source_type=SourceType.LOCAL_FILE, uri="file:///a.md"),
        document=DocumentRef(document_id="d1", title="T"),
        evidence=[Evidence(chunk_id="c1", document_id="d1", hierarchy_level=HierarchyLevel.PARAGRAPH, sequence_index=0)],
        summary="s",
        content={"statement": "s"},
        topics=["t"],
        document_type="report",
        extracted_at=datetime.now(timezone.utc),
        model_used="claude-opus-5",
        pipeline_version="0.1.0",
        source_hash="h1",
    )


def test_writer_produces_manifest_indexing_resources_objects_and_relations(tmp_path):
    writer = OKFWriter(tmp_path)
    src, doc = _sample_source_and_document()
    writer.write_resource(src, doc)

    fact = _sample_object("ko1", KnowledgeObjectType.FACT)
    entity = _sample_object("ko2", KnowledgeObjectType.ENTITY)
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

    manifest_path = writer.write_manifest()["default"]
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))

    assert manifest["resources"] == [{"id": "s1", "type": "local_file", "path": "resources/s1.yaml"}]
    assert {o["id"] for o in manifest["objects"]} == {"ko1", "ko2"}
    fact_entry = next(o for o in manifest["objects"] if o["id"] == "ko1")
    assert fact_entry["type"] == "fact"
    assert fact_entry["path"] == "objects/fact/ko1.yaml"
    assert manifest["relations"] == [{"id": "rel1", "type": "mentions", "path": "relations/rel1.yaml"}]
    assert manifest["pipeline_version"]
    assert manifest["generated_at"]

    # Files on disk are readable, self-contained records (not just index entries),
    # written under the default project_id's subfolder.
    fact_payload = yaml.safe_load((tmp_path / "default" / "objects/fact/ko1.yaml").read_text(encoding="utf-8"))
    assert fact_payload["knowledge_object_id"] == "ko1"
    assert fact_payload["content"]["statement"] == "s"


def test_write_views_indexes_objects_by_topic_document_and_timeline(tmp_path):
    writer = OKFWriter(tmp_path)
    src, doc = _sample_source_and_document()
    writer.write_resource(src, doc)

    fact = _sample_object("ko1", KnowledgeObjectType.FACT)
    fact = fact.model_copy(update={"topics": ["alpha", "beta"], "extracted_at": datetime(2026, 1, 2, tzinfo=timezone.utc)})
    entity = _sample_object("ko2", KnowledgeObjectType.ENTITY)
    entity = entity.model_copy(update={"topics": ["alpha"], "extracted_at": datetime(2026, 1, 1, tzinfo=timezone.utc)})
    writer.write_objects([fact, entity])

    paths = writer.write_views()["default"]
    assert {p.name for p in paths} == {"by_topic.yaml", "by_document.yaml", "timeline.yaml"}

    by_topic = yaml.safe_load((tmp_path / "default" / "views/by_topic.yaml").read_text(encoding="utf-8"))
    assert {e["id"] for e in by_topic["alpha"]} == {"ko1", "ko2"}
    assert {e["id"] for e in by_topic["beta"]} == {"ko1"}

    by_document = yaml.safe_load((tmp_path / "default" / "views/by_document.yaml").read_text(encoding="utf-8"))
    assert by_document["d1"]["title"] == "T"
    assert {e["id"] for e in by_document["d1"]["objects"]} == {"ko1", "ko2"}

    timeline = yaml.safe_load((tmp_path / "default" / "views/timeline.yaml").read_text(encoding="utf-8"))["timeline"]
    assert [e["id"] for e in timeline] == ["ko2", "ko1"]  # chronological (ko2 extracted first)


def test_manifest_reflects_objects_written_across_multiple_writer_instances(tmp_path):
    """Simulates an incremental sync: a second pipeline run reuses the same
    root dir with a fresh OKFWriter - the manifest must include both runs'
    objects, not just the most recent one."""
    OKFWriter(tmp_path).write_objects([_sample_object("ko1", KnowledgeObjectType.FACT)])
    second_writer = OKFWriter(tmp_path)
    second_writer.write_objects([_sample_object("ko2", KnowledgeObjectType.EVENT)])
    manifest_path = second_writer.write_manifest()["default"]

    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert {o["id"] for o in manifest["objects"]} == {"ko1", "ko2"}


def test_writer_routes_each_item_to_its_own_project_subtree(tmp_path):
    """One OKFWriter instance can be handed items from several projects in
    the same run (e.g. a Slack collector spanning several projects' channels,
    RawItem.project_id) - each must land under its own <root>/{project_id}/,
    not get mixed into the writer's construction-time default."""
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

    ko_a = _sample_object("ko-a", KnowledgeObjectType.FACT).model_copy(
        update={"source": SourceRef.from_source(src_a)}
    )
    ko_b = _sample_object("ko-b", KnowledgeObjectType.FACT).model_copy(
        update={"source": SourceRef.from_source(src_b)}
    )
    writer.write_objects([ko_a, ko_b])

    assert (tmp_path / "PRJ-A" / "resources" / "sa.yaml").exists()
    assert (tmp_path / "PRJ-B" / "resources" / "sb.yaml").exists()
    assert (tmp_path / "PRJ-A" / "objects" / "fact" / "ko-a.yaml").exists()
    assert (tmp_path / "PRJ-B" / "objects" / "fact" / "ko-b.yaml").exists()
    # never cross-written into the other project's subtree
    assert not (tmp_path / "PRJ-A" / "resources" / "sb.yaml").exists()
    assert not (tmp_path / "PRJ-B" / "resources" / "sa.yaml").exists()

    manifests = writer.write_manifest()
    assert set(manifests) == {"default", "PRJ-A", "PRJ-B"}
    manifest_a = yaml.safe_load(manifests["PRJ-A"].read_text(encoding="utf-8"))
    assert {o["id"] for o in manifest_a["objects"]} == {"ko-a"}
    manifest_b = yaml.safe_load(manifests["PRJ-B"].read_text(encoding="utf-8"))
    assert {o["id"] for o in manifest_b["objects"]} == {"ko-b"}


def test_write_relations_routes_by_explicit_project_id_argument(tmp_path):
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

    assert (tmp_path / "PRJ-A" / "relations" / "rel-a.yaml").exists()
    assert not (tmp_path / "default" / "relations" / "rel-a.yaml").exists()
