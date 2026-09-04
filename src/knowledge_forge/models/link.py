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
from typing import Any

from pydantic import BaseModel, Field


class LinkType(str, Enum):
    """Extensible vocabulary for resource-level links (see wiki/okf.md's
    "그래프 뷰어 대비" decision for what's actually wired up vs. schema-only)."""

    THREAD_REPLY = "thread_reply"  # e.g. a Slack episode replying to another
    SAME_CHANNEL_SEQUENCE = "same_channel_sequence"  # adjacent episodes, same Slack channel (context/slack.py)
    THREAD_SEQUENCE = "thread_sequence"  # adjacent threads in the same mailbox, by time (context/gmail.py)
    CROSS_SOURCE_REFERENCE = "cross_source_reference"  # e.g. a doc citing a Slack thread
    DUPLICATE_OF = "duplicate_of"  # near-duplicate resource merged into a canonical one
    # Document-to-document edges materialized in Postgres `document_links`
    # (document_links.py, wiki/storage.md). Direction is from -> to:
    PREVIOUS_VERSION = "previous_version"  # newer version -> the version right before it (same version_family)
    ATTACHMENT_OF = "attachment_of"  # attachment file -> the mail/message it came with (schema-only today)
    PARENT_FOLDER = "parent_folder"  # file -> its Drive folder (schema-only: folders aren't Documents yet)
    THREAD_PARENT = "thread_parent"  # Slack thread episode -> the timeline episode holding its parent message


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


class DocumentLink(BaseModel):
    """A directed, typed edge between two *Documents*, persisted in the
    `document_links` table (storage/schema.sql). `Link` above is the OKF
    file-tree view of the same idea keyed by (source_id, document_id)
    pairs; this is the queryable Postgres one keyed by document_id alone,
    so `search/sql.py` can JOIN it against `documents` directly. Read
    `from_document_id --link_type--> to_document_id` the way the type
    name reads: "from has PREVIOUS_VERSION to", "from is an ATTACHMENT_OF
    to", "from's THREAD_PARENT is to". The two sequence types keep the
    direction `pipeline._resolve_links` already gives `Link`: from is the
    *earlier* episode/thread, to is the one that followed it."""

    link_id: str
    from_document_id: str
    to_document_id: str
    link_type: LinkType
    confidence: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
