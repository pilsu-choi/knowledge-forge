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
- facts: a list of standalone, verifiable factual statements found in the chunk.
- entities: named entities mentioned in the chunk (people, organizations, \
products, locations, dates, amounts, etc.), each with a `type` and, when \
useful, a short `description` grounded in the chunk text.
- relations: subject-predicate-object triples describing relationships stated \
in the chunk. Set `confidence` between 0 and 1 to reflect how explicitly the \
text states the relation (1.0 = stated directly, lower = inferred).
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
