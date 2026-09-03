"""Integration coverage for the dedup/supersedes wiring pipeline.py added on
top of run_pipeline (wiki/roadmap.md 5단계 items 2 and 4) - unlike
tests/test_dedup.py and tests/extraction/test_versioning.py (which test the
pure functions in isolation), this exercises them through the actual
Collector -> Parser -> Chunker -> Extractor -> merge flow, with a real
multi-chunk document and a stubbed Extractor/store standing in for the LLM
and Postgres.
"""

from datetime import datetime, timezone

from knowledge_forge.chunking import AdaptiveChunker
from knowledge_forge.collectors.local_file import LocalFileCollector
from knowledge_forge.extraction.base import ExtractionOutcome, Extractor
from knowledge_forge.models.chunk import HierarchyLevel
from knowledge_forge.models.knowledge_object import DocumentRef, Evidence, KnowledgeObject, KnowledgeObjectType
from knowledge_forge.models.relation import RelationType
from knowledge_forge.models.source import SourceRef, SourceType
from knowledge_forge.parsers.registry import default_registry
from knowledge_forge.pipeline import run_pipeline


class _RepeatingFactExtractor(Extractor):
    """Every chunk group extracts into the exact same FACT statement -
    stands in for an LLM finding "the same" claim in more than one chunk of
    one document, which is what dedup.py's merge is meant to catch."""

    async def extract(self, chunks, document, source) -> ExtractionOutcome:
        evidence = [
            Evidence(
                chunk_id=c.chunk_id, document_id=document.document_id, hierarchy_level=c.hierarchy_level, sequence_index=c.sequence_index
            )
            for c in chunks
        ]
        ko = KnowledgeObject(
            knowledge_object_id=f"ko-{chunks[0].chunk_id}",
            type=KnowledgeObjectType.FACT,
            source=SourceRef.from_source(source),
            document=DocumentRef(document_id=document.document_id, title=document.metadata.title),
            evidence=evidence,
            summary="Acme signed the agreement.",
            content={"statement": "Acme signed the agreement."},
            topics=["contracts"],
            document_type="note",
            extracted_at=datetime.now(timezone.utc),
            model_used="fake",
            pipeline_version="0.1.0",
            source_hash=source.content_hash,
        )
        return ExtractionOutcome(objects=[ko], relations=[])


async def test_repeated_claim_across_chunks_of_one_document_is_merged_by_dedup(tmp_path):
    doc_path = tmp_path / "doc.txt"
    doc_path.write_text("First paragraph about Acme.\n\nSecond paragraph, also about Acme.\n\nThird paragraph too.\n")

    collector = LocalFileCollector(root=doc_path)
    registry = default_registry()
    chunker = AdaptiveChunker(max_tokens_per_chunk=5)  # tiny budget forces multiple chunks
    extractor = _RepeatingFactExtractor()

    outcomes = [outcome async for outcome in run_pipeline(collector, registry, chunker, extractor)]

    assert len(outcomes) == 1  # one merged ExtractionOutcome per document
    objects = outcomes[0].objects
    # Every chunk's identical FACT stays addressable (dedup.py no longer
    # drops duplicates - see its docstring) rather than collapsing into one;
    # only the canonical (earliest extracted_at) absorbs the others' evidence.
    assert len(objects) >= 2
    canonical = min(objects, key=lambda ko: (ko.extracted_at, ko.knowledge_object_id))
    assert len(canonical.evidence) >= 2  # evidence absorbed from every merged chunk
    dup_relations = [r for r in outcomes[0].relations if r.type == RelationType.DUPLICATE_OF]
    assert len(dup_relations) == len(objects) - 1
    assert all(r.object_id == canonical.knowledge_object_id for r in dup_relations)


class _FakeStore:
    """Minimal stand-in for PipelineStore: records what the pipeline saves
    and serves back `prior_objects` for list_prior_knowledge_objects(), so
    resolve_supersedes has something to match against without a real
    Postgres instance."""

    def __init__(self, prior_objects):
        self._prior_objects = prior_objects
        self.saved_objects: list[KnowledgeObject] = []
        self.saved_relations = []
        self.embedded_objects: list[KnowledgeObject] | None = None
        self.embedded_model: str | None = None

    async def record_source(self, source, item):
        pass

    async def record_document(self, document):
        pass

    async def record_chunks(self, chunks):
        pass

    async def record_knowledge_objects(self, objects):
        self.saved_objects.extend(objects)

    async def record_relations(self, relations):
        self.saved_relations.extend(relations)

    async def list_prior_knowledge_objects(self, source_id):
        return self._prior_objects

    async def record_embeddings(self, objects, embedder, embedding_model):
        self.embedded_objects = objects
        self.embedded_model = embedding_model


async def test_reextraction_against_a_prior_store_autofills_supersedes(tmp_path):
    doc_path = tmp_path / "doc.txt"
    doc_path.write_text("A short note about Acme.\n")

    collector = LocalFileCollector(root=doc_path)
    registry = default_registry()
    chunker = AdaptiveChunker(max_tokens_per_chunk=4000)
    extractor = _RepeatingFactExtractor()

    prior = KnowledgeObject(
        knowledge_object_id="ko-prior",
        type=KnowledgeObjectType.FACT,
        source=SourceRef(source_id="unused", source_type=SourceType.LOCAL_FILE, uri=doc_path.as_uri()),
        document=DocumentRef(document_id="prior-doc"),
        evidence=[Evidence(chunk_id="prior-c1", document_id="prior-doc", hierarchy_level=HierarchyLevel.PARAGRAPH, sequence_index=0)],
        summary="Acme signed the agreement.",
        content={"statement": "Acme signed the agreement."},
        topics=[],
        document_type="note",
        extracted_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        model_used="fake",
        version=1,
        pipeline_version="0.1.0",
        source_hash="old-hash",
    )
    store = _FakeStore(prior_objects=[prior])

    outcomes = [outcome async for outcome in run_pipeline(collector, registry, chunker, extractor, store=store)]

    assert len(outcomes) == 1
    new_ko = outcomes[0].objects[0]
    assert new_ko.supersedes == "ko-prior"
    assert new_ko.version == 2
    assert new_ko in store.saved_objects


class _FakeEmbedder:
    """Minimal stand-in for EmbeddingProvider - only run_pipeline's call site
    (whether/with-what it's called) is under test here, not the embedding
    math itself (see tests/embedding/ for that)."""

    async def embed(self, texts):
        return [[0.0] for _ in texts]


async def test_embedder_given_embeds_final_merged_objects_after_storing(tmp_path):
    """embedder is only meaningful together with store (nowhere to persist
    embeddings otherwise) - record_embeddings() must see the same final,
    deduped/supersede-resolved object set that was just passed to
    record_knowledge_objects(), not the pre-merge per-group objects."""
    doc_path = tmp_path / "doc.txt"
    doc_path.write_text("A short note about Acme.\n")

    collector = LocalFileCollector(root=doc_path)
    registry = default_registry()
    chunker = AdaptiveChunker(max_tokens_per_chunk=4000)
    extractor = _RepeatingFactExtractor()
    store = _FakeStore(prior_objects=[])
    embedder = _FakeEmbedder()

    outcomes = [
        outcome
        async for outcome in run_pipeline(collector, registry, chunker, extractor, store=store, embedder=embedder)
    ]

    assert len(outcomes) == 1
    assert store.embedded_objects == outcomes[0].objects
    assert store.embedded_model is not None


async def test_embedder_omitted_skips_embedding_even_with_store(tmp_path):
    doc_path = tmp_path / "doc.txt"
    doc_path.write_text("A short note about Acme.\n")

    collector = LocalFileCollector(root=doc_path)
    registry = default_registry()
    chunker = AdaptiveChunker(max_tokens_per_chunk=4000)
    extractor = _RepeatingFactExtractor()
    store = _FakeStore(prior_objects=[])

    [outcome async for outcome in run_pipeline(collector, registry, chunker, extractor, store=store)]

    assert store.embedded_objects is None
