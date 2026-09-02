"""Link: a typed edge between two OKF resources (Source+Document units).

Per the design decision recorded in wiki/okf.md ("그래프 뷰어 대비: 문서 간
링크 설계 결정"), this is deliberately a *separate* record type from
`Relation` rather than an inline `links: [...]` field on `resources/*.yaml`.
`Relation` only ever connects `KnowledgeObject` ids produced within a single
extraction call (see extraction/outcome.py) - it cannot express "this Slack
episode relates to that other episode/document". `Link` fills that gap at
the resource level, so a future graph viewer can walk document-to-document
edges by reading `links/*.yaml` files alone, the same way it already reads
`relations/*.yaml` for object-to-object edges - no database required.
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class LinkType(str, Enum):
    """Extensible vocabulary for resource-level links (see wiki/okf.md's
    "그래프 뷰어 대비" decision for what's actually wired up vs. schema-only)."""

    THREAD_REPLY = "thread_reply"  # e.g. a Slack episode replying to another
    SAME_CHANNEL_SEQUENCE = "same_channel_sequence"  # adjacent episodes, same Slack channel (context/slack.py)
    THREAD_SEQUENCE = "thread_sequence"  # adjacent threads in the same mailbox, by time (context/gmail.py)
    CROSS_SOURCE_REFERENCE = "cross_source_reference"  # e.g. a doc citing a Slack thread
    DUPLICATE_OF = "duplicate_of"  # near-duplicate resource merged into a canonical one


class LinkEndpoint(BaseModel):
    """Points at one resource - the same (Source, Document) pair
    `OKFWriter.write_resource()` files under `resources/{slug}-{source_id8}.md`."""

    source_id: str
    document_id: str


class Link(BaseModel):
    """A directed, typed edge: source_ref --type--> target_ref, between two
    resources (not KnowledgeObjects - see Relation for that)."""

    link_id: str
    type: LinkType
    source_ref: LinkEndpoint
    target_ref: LinkEndpoint
    label: str | None = None
    confidence: float | None = None
    created_at: datetime
