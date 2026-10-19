"""Cross-document concurrency in `run_pipeline` (KF_MAX_CONCURRENT_DOCUMENTS):
documents overlap, but outcomes come back in stream order and each item's
document stage (record_document -> link_document -> OKF links) still runs in
stream order - so a later item's links resolve against an earlier item even
when that earlier item parses slowest."""

import asyncio
from collections.abc import Iterator
from datetime import datetime, timezone

import pytest

from knowledge_forge.chunking import AdaptiveChunker
from knowledge_forge.collectors.base import Collector, Cursor, RawItem
from knowledge_forge.config import settings
from knowledge_forge.extraction.base import ExtractionOutcome, Extractor
from knowledge_forge.models.chunk import HierarchyLevel
from knowledge_forge.models.document import Document, TextBlock
from knowledge_forge.models.knowledge_object import DocumentRef, Evidence, KnowledgeObject, KnowledgeObjectType
from knowledge_forge.models.link import LinkType
from knowledge_forge.models.source import SourceRef, SourceType
from knowledge_forge.pipeline import run_pipeline

# uri -> seconds its parse and its extraction take; "a" is slowest, so a
# first-come ordering would put it last.
_DELAYS = {"test://a": 0.05, "test://b": 0.01, "test://c": 0.0}


class _Collector(Collector):
    source_type = SourceType.LOCAL_FILE

    def initial_sync(self) -> Iterator[RawItem]:
        for uri in _DELAYS:
            links = [{"type": LinkType.THREAD_SEQUENCE.value, "target_uri": "test://a"}] if uri == "test://b" else []
            yield RawItem(
                uri=uri,
                content=uri.encode(),
                content_hash=f"hash-{uri}",
                modified_at=datetime.now(timezone.utc),
                metadata={"links": links},
            )

    def incremental_sync(self, cursor: Cursor) -> Iterator[RawItem]:
        return self.initial_sync()

    def get_cursor(self) -> Cursor:
        return Cursor()


class _SlowParser:
    def __init__(self):
        self.active = 0
        self.max_active = 0

    def get_parser_for(self, uri, filename=None):
        return self

    async def parse(self, item: RawItem, source_ref: SourceRef) -> Document:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(_DELAYS[item.uri])
        self.active -= 1
        return Document(
            document_id=f"doc-{item.uri}",
            source=source_ref,
            elements=[TextBlock(element_id="e1", text=f"text of {item.uri}")],
        )


class _Extractor(Extractor):
    async def extract(self, chunks, document, source) -> ExtractionOutcome:
        await asyncio.sleep(_DELAYS[source.uri])
        ko = KnowledgeObject(
            knowledge_object_id=f"ko-{source.uri}",
            type=KnowledgeObjectType.FACT,
            source=SourceRef.from_source(source),
            document=DocumentRef(document_id=document.document_id),
            evidence=[
                Evidence(chunk_id=chunks[0].chunk_id, document_id=document.document_id,
                         hierarchy_level=HierarchyLevel.PARAGRAPH, sequence_index=0)
            ],
            summary=f"fact from {source.uri}",
            content={"statement": f"fact from {source.uri}"},
            topics=[],
            document_type="note",
            extracted_at=datetime.now(timezone.utc),
            model_used="fake",
            pipeline_version="0",
            source_hash=source.content_hash,
        )
        return ExtractionOutcome(objects=[ko], relations=[])


class _Store:
    def __init__(self, fail_on_document: str | None = None):
        self.fail_on_document = fail_on_document
        self.documents: list[str] = []
        self.linked_after: dict[str, list[str]] = {}

    async def get_source_content_hash(self, source_id):
        return None

    async def record_source(self, source, item):
        pass

    async def record_document(self, document):
        await asyncio.sleep(0)
        if document.document_id == self.fail_on_document:
            raise RuntimeError("database went away")
        self.documents.append(document.document_id)

    async def link_document(self, source, document):
        self.linked_after[document.document_id] = list(self.documents)
        return []

    async def record_chunks(self, document_id, chunks):
        pass

    async def list_prior_knowledge_objects(self, source_id):
        return []

    async def record_knowledge_objects(self, objects):
        pass

    async def record_relations(self, relations):
        pass

    async def record_entities(self, resolution):
        pass


class _OKF:
    def __init__(self):
        self.links = []

    def write_resource(self, source, document):
        pass

    def write_links(self, links, project_id):
        self.links.extend(links)

    def write_objects(self, objects):
        pass

    def write_relations(self, relations, project_id):
        pass


@pytest.mark.parametrize("concurrency", [1, 4])
async def test_documents_overlap_but_outcomes_and_document_stage_keep_stream_order(monkeypatch, concurrency):
    monkeypatch.setattr(settings, "max_concurrent_documents", concurrency)
    parser, store, okf = _SlowParser(), _Store(), _OKF()

    outcomes = [
        outcome
        async for outcome in run_pipeline(
            _Collector(), parser, AdaptiveChunker(max_tokens_per_chunk=100), _Extractor(), store=store, okf_writer=okf
        )
    ]

    assert [o.objects[0].source.uri for o in outcomes] == list(_DELAYS)
    assert store.documents == [f"doc-{uri}" for uri in _DELAYS]
    # b's link_document ran after a's document row existed, and its OKF link resolved to a.
    assert store.linked_after["doc-test://b"] == ["doc-test://a", "doc-test://b"]
    assert [(link.source_ref.document_id, link.target_ref.document_id) for link in okf.links] == [
        ("doc-test://a", "doc-test://b")
    ]
    assert parser.max_active == min(concurrency, len(_DELAYS))


async def test_a_failure_is_raised_in_stream_order_after_earlier_outcomes(monkeypatch):
    monkeypatch.setattr(settings, "max_concurrent_documents", 4)
    store = _Store(fail_on_document="doc-test://b")
    outcomes = []

    with pytest.raises(RuntimeError, match="database went away"):
        async for outcome in run_pipeline(
            _Collector(), _SlowParser(), AdaptiveChunker(max_tokens_per_chunk=100), _Extractor(), store=store
        ):
            outcomes.append(outcome)

    assert [o.objects[0].source.uri for o in outcomes] == ["test://a"]
