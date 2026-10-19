from datetime import datetime, timezone

import pytest

from knowledge_forge.entity_resolution import (
    normalize_entity_name,
    normalize_entity_type,
    resolve_entities,
    split_names,
)
from knowledge_forge.ids import stable_id
from knowledge_forge.models.chunk import HierarchyLevel
from knowledge_forge.models.entity import MentionRole
from knowledge_forge.models.knowledge_object import DocumentRef, Evidence, KnowledgeObject, KnowledgeObjectType
from knowledge_forge.models.relation import Relation, RelationType
from knowledge_forge.models.source import SourceRef, SourceType


def _ko(ko_id: str, type_: KnowledgeObjectType, content: dict, project_id: str = "p1") -> KnowledgeObject:
    return KnowledgeObject(
        knowledge_object_id=ko_id,
        type=type_,
        source=SourceRef(source_id="s1", source_type=SourceType.SLACK, uri="slack://x", project_id=project_id),
        document=DocumentRef(document_id="d1"),
        evidence=[Evidence(chunk_id=f"c-{ko_id}", document_id="d1", hierarchy_level=HierarchyLevel.PARAGRAPH, sequence_index=0)],
        summary="s",
        content=content,
        document_type="slack",
        extracted_at=datetime.now(timezone.utc),
        model_used="m",
        pipeline_version="0",
        source_hash="h",
    )


def _entity(ko_id: str, name: str, entity_type: str = "PERSON", project_id: str = "p1") -> KnowledgeObject:
    return _ko(ko_id, KnowledgeObjectType.ENTITY, {"name": name, "entity_type": entity_type}, project_id)


@pytest.mark.parametrize(
    "surface, expected",
    [
        ("정민찬", "정민찬"),
        ("정민찬 팀장님", "정민찬"),
        ("정민찬팀장", "정민찬"),
        ("정민찬(PM)", "정민찬"),
        ("정민찬 PM", "정민찬"),
        ("정 민찬", "정민찬"),
        ("Dr. John  Smith", "johnsmith"),
        ("김이사", "김이사"),  # glued title would leave a bare surname - kept whole
        ("김 이사", None),  # bare surname - ambiguous, not resolved
        ("팀장님", None),
        ("담당자", None),
        ("", None),
    ],
)
def test_person_name_normalization_is_conservative(surface, expected):
    assert normalize_entity_name(surface, "person") == expected


def test_organization_markers_are_folded_only_for_organizations():
    assert normalize_entity_name("㈜에이지소다", "organization") == "에이지소다"
    assert normalize_entity_name("주식회사 에이지소다", "organization") == "에이지소다"
    assert normalize_entity_name("에이지소다 팀장", "organization") == "에이지소다팀장"  # person rules don't apply


def test_entity_type_folds_known_synonyms_and_keeps_unknown_labels_distinct():
    assert {normalize_entity_type(t) for t in ["PERSON", "인물", "사람", " person "]} == {"person"}
    assert {normalize_entity_type(t) for t in ["ORG", "회사", "Organization"]} == {"organization"}
    assert normalize_entity_type("Software Tool") == "software_tool"


def test_split_names_separates_packed_people():
    assert split_names(["정민찬, 김철수", "박영희 및 이수진"]) == ["정민찬", "김철수", "박영희", "이수진"]


def test_same_person_across_objects_and_attributes_resolves_to_one_entity():
    objects = [
        _entity("e1", "정민찬"),
        _entity("e2", "정민찬 팀장님", entity_type="인물"),
        _ko("dec", KnowledgeObjectType.DECISION, {"statement": "s", "decided_by": ["정민찬 팀장"]}),
        _ko("act", KnowledgeObjectType.ACTION_ITEM, {"statement": "s", "assignees": ["정민찬, 김철수"]}),
        _ko("fact", KnowledgeObjectType.FACT, {"statement": "s"}),
    ]
    relations = [
        Relation(relation_id="r1", type=RelationType.MENTIONS, subject_id="fact", object_id="e1",
                 created_at=datetime.now(timezone.utc)),
    ]

    resolution = resolve_entities(objects, relations)

    by_name = {e.name: e for e in resolution.entities}
    assert set(by_name) == {"정민찬", "김철수"}
    jung = by_name["정민찬"]
    assert jung.entity_id == stable_id("entity", "p1", "person", "정민찬")
    assert jung.display_name == "정민찬"
    assert jung.aliases == ["정민찬", "정민찬 팀장님", "정민찬 팀장"]
    roles = {(m.knowledge_object_id, m.role) for m in resolution.mentions if m.entity_id == jung.entity_id}
    assert roles == {
        ("e1", MentionRole.ENTITY),
        ("e2", MentionRole.ENTITY),
        ("dec", MentionRole.DECIDED_BY),
        ("act", MentionRole.ASSIGNEE),
        ("fact", MentionRole.MENTIONS),
    }
    fact_mention = next(m for m in resolution.mentions if m.knowledge_object_id == "fact")
    assert fact_mention.evidence == ["c-fact"]


def test_resolution_never_crosses_projects_or_types_and_skips_values():
    resolution = resolve_entities(
        [
            _entity("a", "정민찬", project_id="p1"),
            _entity("b", "정민찬", project_id="p2"),
            _entity("c", "Forge", entity_type="PRODUCT"),
            _entity("d", "Forge", entity_type="ORG"),
            _entity("e", "2026-10-19", entity_type="DATE"),
            _entity("f", "3억 원", entity_type="금액"),
        ],
        [],
    )
    keys = {(e.project_id, e.entity_type, e.name) for e in resolution.entities}
    assert keys == {("p1", "person", "정민찬"), ("p2", "person", "정민찬"), ("p1", "product", "forge"),
                    ("p1", "organization", "forge")}
