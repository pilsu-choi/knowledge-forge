"""`run_pipeline`'s unchanged-source skip (item 1 of wiki/reingest-replay.md's
"변경 없는 문서 재처리" fix): when `store` is given and a source's
`content_hash` already matches a stored source that produced a `documents`
row, the item is skipped entirely - no parse, no chunk, no extraction call,
no `ExtractionOutcome`. `force=True` (what `reparse` passes) bypasses the
check."""

from knowledge_forge.chunking import AdaptiveChunker
from knowledge_forge.collectors.local_file import LocalFileCollector
from knowledge_forge.extraction.base import ExtractionOutcome, Extractor
from knowledge_forge.ids import content_hash
from knowledge_forge.parsers.registry import default_registry
from knowledge_forge.pipeline import run_pipeline


class _CountingExtractor(Extractor):
    def __init__(self):
        self.calls = 0

    async def extract(self, chunks, document, source) -> ExtractionOutcome:
        self.calls += 1
        return ExtractionOutcome(objects=[], relations=[])


class _FakeUnchangedStore:
    """Reports `stored_hash` as the content_hash of an already-processed
    source (i.e. as if a prior run produced a `documents` row for it) -
    every write method just records that it was called, so a skip can be
    asserted by checking nothing was recorded."""

    def __init__(self, stored_hash: str | None):
        self.stored_hash = stored_hash
        self.calls: list[str] = []

    async def get_source_content_hash(self, source_id):
        return self.stored_hash

    async def record_source(self, source, item):
        self.calls.append("record_source")

    async def record_document(self, document):
        self.calls.append("record_document")

    async def link_document(self, source, document):
        self.calls.append("link_document")
        return []

    async def record_chunks(self, chunks):
        self.calls.append("record_chunks")

    async def record_knowledge_objects(self, objects):
        self.calls.append("record_knowledge_objects")

    async def record_relations(self, relations):
        self.calls.append("record_relations")

    async def list_prior_knowledge_objects(self, source_id):
        return []


async def test_unchanged_source_is_skipped_with_no_outcome_and_no_extraction(tmp_path):
    doc_path = tmp_path / "doc.txt"
    doc_path.write_text("Unchanged content.\n")
    matching_hash = content_hash(doc_path.read_bytes())

    collector = LocalFileCollector(root=doc_path)
    registry = default_registry()
    chunker = AdaptiveChunker(max_tokens_per_chunk=4000)
    extractor = _CountingExtractor()
    store = _FakeUnchangedStore(stored_hash=matching_hash)

    outcomes = [outcome async for outcome in run_pipeline(collector, registry, chunker, extractor, store=store)]

    assert outcomes == []
    assert extractor.calls == 0
    # the skip happens before record_source, so a skipped item rewrites nothing
    assert store.calls == []


async def test_changed_content_hash_is_not_skipped(tmp_path):
    doc_path = tmp_path / "doc.txt"
    doc_path.write_text("New content.\n")

    collector = LocalFileCollector(root=doc_path)
    registry = default_registry()
    chunker = AdaptiveChunker(max_tokens_per_chunk=4000)
    extractor = _CountingExtractor()
    store = _FakeUnchangedStore(stored_hash="some-other-hash")

    outcomes = [outcome async for outcome in run_pipeline(collector, registry, chunker, extractor, store=store)]

    assert len(outcomes) == 1
    assert extractor.calls == 1
    assert "record_source" in store.calls


async def test_no_prior_document_is_not_skipped(tmp_path):
    doc_path = tmp_path / "doc.txt"
    doc_path.write_text("First time seeing this.\n")

    collector = LocalFileCollector(root=doc_path)
    registry = default_registry()
    chunker = AdaptiveChunker(max_tokens_per_chunk=4000)
    extractor = _CountingExtractor()
    store = _FakeUnchangedStore(stored_hash=None)  # no documents row yet

    outcomes = [outcome async for outcome in run_pipeline(collector, registry, chunker, extractor, store=store)]

    assert len(outcomes) == 1
    assert extractor.calls == 1


async def test_force_bypasses_the_skip_for_an_unchanged_source(tmp_path):
    doc_path = tmp_path / "doc.txt"
    doc_path.write_text("Unchanged content.\n")
    matching_hash = content_hash(doc_path.read_bytes())

    collector = LocalFileCollector(root=doc_path)
    registry = default_registry()
    chunker = AdaptiveChunker(max_tokens_per_chunk=4000)
    extractor = _CountingExtractor()
    store = _FakeUnchangedStore(stored_hash=matching_hash)

    outcomes = [
        outcome
        async for outcome in run_pipeline(collector, registry, chunker, extractor, store=store, force=True)
    ]

    assert len(outcomes) == 1
    assert extractor.calls == 1
    assert "record_source" in store.calls
