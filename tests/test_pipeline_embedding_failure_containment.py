"""Regression coverage for the reparse bug where an embedding call that
failed (e.g. a request rejected as too large) propagated all the way out of
`run_pipeline`'s async generator, aborting an entire batch after its first
oversized document - one google_drive reparse died outright on this
(wiki/reingest-replay.md). A Parser failure was already contained
(pipeline.py's `except Exception` around `parser.parse`); embedding now gets
the same treatment: log and move on to the next document, keeping the
Document/Chunks already written for the failing one.
"""

from knowledge_forge.chunking import AdaptiveChunker
from knowledge_forge.collectors.local_file import LocalFileCollector
from knowledge_forge.parsers.registry import default_registry
from knowledge_forge.pipeline import run_pipeline


class _FailFirstEmbedder:
    """Raises on its first call (standing in for a provider 400), then
    succeeds - so exactly one of two documents fails to embed."""

    def __init__(self):
        self.calls = 0

    async def embed(self, texts):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("request rejected: too large")
        return [[0.0] for _ in texts]


class _RecordingStore:
    """Mirrors PipelineStore's chunk-embedding call shape closely enough to
    exercise pipeline.py's try/except around it - real error propagation
    (storage/store.py's record_chunk_embeddings has no try/except of its
    own), just without a database."""

    def __init__(self, embedder):
        self._embedder = embedder
        self.documents: list[str] = []
        self.chunks_recorded: list[str] = []
        self.embedded_documents: list[str] = []

    async def record_source(self, source, item):
        pass

    async def get_source_content_hash(self, source_id):
        return None

    async def record_document(self, document):
        self.documents.append(document.document_id)

    async def link_document(self, source, document):
        return []

    async def record_chunks(self, chunks):
        self.chunks_recorded.extend(c.chunk_id for c in chunks)

    async def record_chunk_embeddings(self, chunks, project_id, embedder, embedding_model):
        await embedder.embed([c.text for c in chunks])  # raises exactly like the real store on a rejected request
        self.embedded_documents.extend(c.document_id for c in chunks)


async def test_one_documents_embedding_failure_does_not_abort_the_run(tmp_path):
    (tmp_path / "a.txt").write_text("First document, whose embedding call will fail.\n")
    (tmp_path / "b.txt").write_text("Second document, which should still get embedded.\n")

    collector = LocalFileCollector(root=tmp_path)
    embedder = _FailFirstEmbedder()
    store = _RecordingStore(embedder)

    outcomes = [
        outcome
        async for outcome in run_pipeline(
            collector,
            default_registry(),
            AdaptiveChunker(max_tokens_per_chunk=100),
            extractor=None,
            store=store,
            embedder=embedder,
            embedding_source="chunk",
        )
    ]

    # Both documents were processed - the run did not abort after the first
    # embedding failure.
    assert len(outcomes) == 2
    assert len(store.documents) == 2
    assert len(store.chunks_recorded) > 0
    # The failing document's Document/Chunks are still kept...
    assert embedder.calls == 2
    # ...but only the document whose embedding call succeeded has embeddings.
    assert len(store.embedded_documents) == 1
