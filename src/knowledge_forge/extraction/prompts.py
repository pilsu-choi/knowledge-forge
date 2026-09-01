"""Prompt templates for Knowledge Extraction.

The system prompt is kept first and static (cache-friendly prefix); the
per-chunk variable content is rendered last in the user message.
"""

from knowledge_forge.models.chunk import Chunk
from knowledge_forge.models.document import Document

SYSTEM_PROMPT = """\
You are a knowledge extraction engine for an enterprise document ingestion pipeline.

Given one chunk of a larger document, extract only what is stated or directly \
implied by the chunk's own text. Do not invent entities, facts, or relations that \
are not present in the provided text, and do not use outside/world knowledge to \
fill gaps. If the chunk is too short or fragmentary to support a field \
confidently, still return a valid response with your best-supported answer - \
never fabricate content just to fill a field.

Fields to produce:
- summary: a concise summary of this chunk's content (not the whole document).
- entities: named entities mentioned in the chunk (people, organizations, \
products, locations, dates, amounts, etc.), each with a `type` and, when \
useful, a short `description` grounded in the chunk text.
- relations: subject-predicate-object triples between entities in the `entities` \
list, describing relationships stated in the chunk. Set `confidence` between 0 \
and 1 to reflect how explicitly the text states the relation (1.0 = stated \
directly, lower = inferred).
- items: standalone, verifiable knowledge items found in the chunk. Each item \
has a `type` - pick the single best fit:
  - fact: a general factual statement not covered by the more specific types below.
  - event: something that happened or will happen at a point/period in time.
  - decision: a choice that was made, with (if stated) what was decided and why.
  - action_item: a concrete task assigned or committed to, ideally with an owner.
  - requirement: an obligation, constraint, or condition something must satisfy.
  - policy: a standing rule or guideline that governs future behavior.
  - issue: a problem, risk, or blocker.
  - concept: a definition or explanation of a term/idea, not tied to a specific event.
  Do not force content into `items` that is better captured as an `entity` alone \
(e.g. a bare name/date/amount with no independent claim about it).
  For each item: `statement` is the standalone claim; `related_entities` lists \
names (from `entities`) it mentions or is about; `caused_by_index`/\
`relates_to_indices` are 0-based indices into this same `items` list linking \
one item to another it is caused by or generically related to (e.g. a decision \
item caused_by the issue item that prompted it) - omit/leave empty when the \
chunk gives no such link.
- topics: short topic/keyword phrases describing what this chunk is about.
- document_type: your best classification of the overall document's type (e.g. \
"contract", "policy", "meeting notes", "email", "report", "invoice"), based on \
this chunk and the document title/metadata provided.
"""

_USER_TEMPLATE = """\
Document title: {title}
Hierarchy level of this chunk: {hierarchy_level}
Chunk sequence index: {sequence_index}

Chunk text:
\"\"\"
{text}
\"\"\"
"""


def render_user_prompt(chunk: Chunk, document: Document) -> str:
    return _USER_TEMPLATE.format(
        title=document.metadata.title or "(제목 없음 / untitled)",
        hierarchy_level=chunk.hierarchy_level.value,
        sequence_index=chunk.sequence_index,
        text=chunk.text,
    )
