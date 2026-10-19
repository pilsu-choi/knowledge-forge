"""Rule-based entity resolution (wiki/improvement-plan-2026-09-15.md O1).

Turns the mention-level ENTITY KnowledgeObjects of one extraction run (plus
the people named in decision/action_item/event attributes) into project-
scoped `Entity` nodes and `EntityMention` rows (models/entity.py).

Precision over recall, deliberately: two surface forms merge only when
their normalized `(project_id, entity_type, name)` key is identical, and
the normalization below is a short list of rules that are safe for Korean
business text - NFC, whitespace, honorifics/job titles on people,
corporate markers on organizations. Anything that would need judgement
("정민찬" vs "Minchan Jeong", "김 팀장" with no given name) stays split or
is dropped rather than guessed; a wrong merge poisons every consumer that
expands by entity, a missed one only costs recall. No LLM call - a later
pass can merge entities on top of these ids without changing them.

Like dedup.py this is a pure function over in-memory objects; pipeline.py
calls it per document and the store's upsert merges aliases across runs.
"""

import re
from dataclasses import dataclass, field

from knowledge_forge.ids import stable_id
from knowledge_forge.models.entity import Entity, EntityMention, MentionRole
from knowledge_forge.models.knowledge_object import KnowledgeObject, KnowledgeObjectType
from knowledge_forge.models.relation import Relation, RelationType
from knowledge_forge.text_normalize import to_nfc

# The LLM's entity_type is free text ("PERSON", "인물", "사람"...). Only
# well-known synonyms are folded; anything else is kept as its casefolded
# self, so an unexpected type never merges into a different one.
_TYPE_SYNONYMS = {
    **dict.fromkeys(["person", "per", "people", "human", "인물", "사람", "개인", "직원", "담당자"], "person"),
    **dict.fromkeys(
        ["organization", "organisation", "org", "company", "corporation", "team", "department",
         "조직", "회사", "기업", "기관", "법인", "부서", "팀"],
        "organization",
    ),
    **dict.fromkeys(["product", "service", "solution", "제품", "서비스", "솔루션"], "product"),
    **dict.fromkeys(["location", "loc", "place", "gpe", "장소", "위치", "지역"], "location"),
    **dict.fromkeys(["project", "프로젝트"], "project"),
    **dict.fromkeys(["date", "time", "datetime", "period", "날짜", "일자", "일시", "시간", "기간"], "date"),
    **dict.fromkeys(
        ["amount", "money", "number", "quantity", "percent", "percentage", "금액", "수량", "수치", "비율"],
        "amount",
    ),
}
# Values, not things: "2026-03-01" or "3억 원" in two documents are not the
# same node in any useful sense, so they are not resolved at all.
VALUE_TYPES = frozenset({"date", "amount"})

# Longest first, so "대표이사" is stripped before "이사" could match its tail.
_PERSON_TITLES = sorted(
    ["님", "씨", "대표이사", "대표", "부사장", "사장", "부회장", "회장", "전무", "상무", "이사", "본부장",
     "센터장", "실장", "부장", "차장", "과장", "대리", "주임", "사원", "팀장", "파트장", "그룹장", "매니저",
     "책임", "선임", "수석", "연구원", "위원", "교수", "박사", "pm", "pl"],
    key=len,
    reverse=True,
)
_EN_PERSON_PREFIX = re.compile(r"^(?:mr|mrs|ms|dr|prof)\.?\s+", re.IGNORECASE)
_PARENTHETICAL = re.compile(r"\s*[(（\[][^)）\]]*[)）\]]")
_ORG_MARKERS = re.compile(r"㈜|\(주\)|\(유\)|주식회사|유한회사")
_HANGUL = re.compile(r"^[가-힣]+$")
_WHITESPACE = re.compile(r"\s+")
# A name that only says *which role*, not *who* - never an entity.
_GENERIC_NAMES = frozenset(
    ["담당자", "참석자", "참여자", "전원", "모두", "고객", "고객사", "우리", "본인", "당사", "회사", "팀",
     "미정", "tbd", "n/a", "none", "unknown"]
)
# Separators between several people packed into one attribute string
# ("정민찬, 김철수" / "정민찬 및 김철수").
_NAME_SEPARATORS = re.compile(r"\s*(?:[,/、·&;]|\s및\s|\sand\s)\s*")


def normalize_entity_type(raw: str | None) -> str:
    key = _WHITESPACE.sub("_", to_nfc(raw or "").strip().casefold())
    return _TYPE_SYNONYMS.get(key, key)


def clean_surface(text: str) -> str:
    return _WHITESPACE.sub(" ", to_nfc(text)).strip()


def _strip_person_titles(name: str) -> str:
    """Drops trailing honorifics/job titles ("정민찬 팀장님" -> "정민찬").
    A title glued to the word before it (no space) is only dropped when
    that word is still a 2+ syllable Hangul name - "김이사" stays as-is
    rather than becoming the bare surname "김"."""
    changed = True
    while changed:
        changed = False
        for title in _PERSON_TITLES:
            if not name.casefold().endswith(title) or len(name) == len(title):
                continue
            head = name[: -len(title)]
            spaced = head.endswith(" ")
            head = head.rstrip()
            last_word = head.rsplit(" ", 1)[-1]
            if spaced or (not title.isascii() and _HANGUL.match(last_word) and len(last_word) >= 2):
                name, changed = head, True
                break
    return name


def normalize_entity_name(surface: str, entity_type: str) -> str | None:
    """The resolution key for `surface` under an already-normalized
    `entity_type`, or None when the surface doesn't safely identify one
    entity (empty, a bare title/role word, a single Hangul syllable)."""
    name = clean_surface(surface)
    if entity_type == "person":
        name = _EN_PERSON_PREFIX.sub("", name)
        name = _PARENTHETICAL.sub("", name).strip() or name
        name = _strip_person_titles(name)
    elif entity_type == "organization":
        name = _ORG_MARKERS.sub(" ", name).strip()
    key = _WHITESPACE.sub("", name).casefold()
    if not key or key in _GENERIC_NAMES or key in _PERSON_TITLES:
        return None
    if len(key) == 1 and _HANGUL.match(key):
        return None
    return key


def split_names(values: list[str]) -> list[str]:
    return [part for value in values for part in _NAME_SEPARATORS.split(clean_surface(value)) if part]


@dataclass
class EntityResolution:
    entities: list[Entity] = field(default_factory=list)
    mentions: list[EntityMention] = field(default_factory=list)


# Person-valued attributes of the typed item contents (models/knowledge_object.py).
_PERSON_ATTRIBUTES = {
    KnowledgeObjectType.DECISION: ("decided_by", MentionRole.DECIDED_BY),
    KnowledgeObjectType.ACTION_ITEM: ("assignees", MentionRole.ASSIGNEE),
    KnowledgeObjectType.EVENT: ("participants", MentionRole.PARTICIPANT),
}


def resolve_entities(objects: list[KnowledgeObject], relations: list[Relation]) -> EntityResolution:
    """Entities and mentions for one batch of objects. Every entity id is
    `stable_id("entity", project_id, entity_type, name)`, so the same name
    resolved in another document (or another run) lands on the same row -
    the store's upsert is what accumulates aliases across batches."""
    entities: dict[str, Entity] = {}
    mentions: dict[tuple[str, str, MentionRole], EntityMention] = {}

    def add(ko: KnowledgeObject, surface: str, raw_type: str | None, role: MentionRole) -> str | None:
        entity_type = normalize_entity_type(raw_type)
        if entity_type in VALUE_TYPES or not entity_type:
            return None
        name = normalize_entity_name(surface, entity_type)
        if name is None:
            return None
        project_id = ko.source.project_id
        entity_id = stable_id("entity", project_id, entity_type, name)
        display = clean_surface(surface)
        entity = entities.setdefault(
            entity_id,
            Entity(entity_id=entity_id, project_id=project_id, entity_type=entity_type, name=name, display_name=display),
        )
        if display not in entity.aliases:
            entity.aliases.append(display)
        mentions.setdefault(
            (entity_id, ko.knowledge_object_id, role),
            EntityMention(
                entity_id=entity_id,
                knowledge_object_id=ko.knowledge_object_id,
                role=role,
                surface_form=display,
                evidence=[e.chunk_id for e in ko.evidence],
            ),
        )
        return entity_id

    entity_by_ko: dict[str, tuple[str, str | None]] = {}
    for ko in objects:
        if ko.type == KnowledgeObjectType.ENTITY:
            surface = ko.content.get("name") or ""
            raw_type = ko.content.get("entity_type") or ko.content.get("type")
            if add(ko, surface, raw_type, MentionRole.ENTITY):
                entity_by_ko[ko.knowledge_object_id] = (surface, raw_type)
        elif ko.type in _PERSON_ATTRIBUTES:
            attribute, role = _PERSON_ATTRIBUTES[ko.type]
            for person in split_names(ko.content.get(attribute) or []):
                add(ko, person, "person", role)

    # An item that MENTIONS an entity object mentions its resolved Entity -
    # this is what makes "every decision about 정민찬" one join away.
    objects_by_id = {ko.knowledge_object_id: ko for ko in objects}
    for rel in relations:
        if rel.type != RelationType.MENTIONS or rel.object_id not in entity_by_ko:
            continue
        subject = objects_by_id.get(rel.subject_id)
        if subject is not None:
            surface, raw_type = entity_by_ko[rel.object_id]
            add(subject, surface, raw_type, MentionRole.MENTIONS)

    return EntityResolution(entities=list(entities.values()), mentions=list(mentions.values()))
