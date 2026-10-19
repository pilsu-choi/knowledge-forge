"""Entity: a resolved, project-scoped ontology node, and EntityMention, the
join from it to the KnowledgeObjects that name it.

An ENTITY-typed KnowledgeObject is one extraction's *mention* ("정민찬
팀장" in one chunk bundle) - the same person extracted from 36 documents
is 36 such objects. `Entity` is the one node they resolve to
(entity_resolution.py), so a consumer can ask "everything about 정민찬"
without string-matching summaries. Resolution never crosses `project_id`.
"""

from enum import Enum

from pydantic import BaseModel, Field


class MentionRole(str, Enum):
    """How a KnowledgeObject refers to an Entity."""

    ENTITY = "entity"  # the ENTITY KnowledgeObject itself
    MENTIONS = "mentions"  # an item with a MENTIONS relation to that ENTITY object
    DECIDED_BY = "decided_by"  # DecisionContent.decided_by
    ASSIGNEE = "assignee"  # ActionItemContent.assignees
    PARTICIPANT = "participant"  # EventContent.participants


class Entity(BaseModel):
    # stable_id("entity", project_id, entity_type, name)
    entity_id: str
    project_id: str
    # Normalized type vocabulary (entity_resolution.normalize_entity_type).
    entity_type: str
    # The normalized resolution key (entity_resolution.normalize_entity_name).
    name: str
    # First surface form seen - what a UI shows.
    display_name: str
    # Every distinct surface form resolved to this entity.
    aliases: list[str] = Field(default_factory=list)


class EntityMention(BaseModel):
    entity_id: str
    knowledge_object_id: str
    role: MentionRole
    surface_form: str
    # chunk_ids grounding the mention - the KnowledgeObject's own evidence.
    evidence: list[str] = Field(default_factory=list)
