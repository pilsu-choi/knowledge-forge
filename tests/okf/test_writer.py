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

    manifest_path = writer.write_manifest()
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))

    assert manifest["resources"] == [{"id": "s1", "type": "local_file", "path": "resources/s1.yaml"}]
    assert {o["id"] for o in manifest["objects"]} == {"ko1", "ko2"}
    fact_entry = next(o for o in manifest["objects"] if o["id"] == "ko1")
    assert fact_entry["type"] == "fact"
    assert fact_entry["path"] == "objects/fact/ko1.yaml"
    assert manifest["relations"] == [{"id": "rel1", "type": "mentions", "path": "relations/rel1.yaml"}]
    assert manifest["pipeline_version"]
    assert manifest["generated_at"]

    # Files on disk are readable, self-contained records (not just index entries).
    fact_payload = yaml.safe_load((tmp_path / "objects/fact/ko1.yaml").read_text(encoding="utf-8"))
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

    paths = writer.write_views()
    assert {p.name for p in paths} == {"by_topic.yaml", "by_document.yaml", "timeline.yaml"}

    by_topic = yaml.safe_load((tmp_path / "views/by_topic.yaml").read_text(encoding="utf-8"))
    assert {e["id"] for e in by_topic["alpha"]} == {"ko1", "ko2"}
    assert {e["id"] for e in by_topic["beta"]} == {"ko1"}

    by_document = yaml.safe_load((tmp_path / "views/by_document.yaml").read_text(encoding="utf-8"))
    assert by_document["d1"]["title"] == "T"
    assert {e["id"] for e in by_document["d1"]["objects"]} == {"ko1", "ko2"}

    timeline = yaml.safe_load((tmp_path / "views/timeline.yaml").read_text(encoding="utf-8"))["timeline"]
    assert [e["id"] for e in timeline] == ["ko2", "ko1"]  # chronological (ko2 extracted first)


def test_manifest_reflects_objects_written_across_multiple_writer_instances(tmp_path):
    """Simulates an incremental sync: a second pipeline run reuses the same
    root dir with a fresh OKFWriter - the manifest must include both runs'
    objects, not just the most recent one."""
    OKFWriter(tmp_path).write_objects([_sample_object("ko1", KnowledgeObjectType.FACT)])
    second_writer = OKFWriter(tmp_path)
    second_writer.write_objects([_sample_object("ko2", KnowledgeObjectType.EVENT)])
    manifest_path = second_writer.write_manifest()

    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert {o["id"] for o in manifest["objects"]} == {"ko1", "ko2"}
