"""End-to-end proof for item 3 of wiki/adaptive-chunking.md's `fix/etl-quick`
section: NFC normalization now happens at chunk construction (the chunker's
output), not only when `PostgresStore.save_chunks` writes the DB row - so
every in-memory consumer of `Chunk.text` (Knowledge Extraction, chunk
embeddings) already sees NFC even though the parsed document text is NFD."""

import unicodedata

from knowledge_forge.chunking import AdaptiveChunker
from knowledge_forge.collectors.local_file import LocalFileCollector
from knowledge_forge.extraction.base import ExtractionOutcome, Extractor
from knowledge_forge.parsers.registry import default_registry
from knowledge_forge.pipeline import run_pipeline


class _CapturingExtractor(Extractor):
    """Records the exact chunk text it was handed, instead of doing any real
    extraction - lets the test assert on what Knowledge Extraction would
    have seen."""

    def __init__(self):
        self.seen_chunk_texts: list[str] = []

    async def extract(self, chunks, document, source) -> ExtractionOutcome:
        self.seen_chunk_texts.extend(c.text for c in chunks)
        return ExtractionOutcome(objects=[], relations=[])


async def test_extractor_receives_nfc_text_even_though_the_source_file_is_nfd(tmp_path):
    nfd_body = unicodedata.normalize("NFD", "한국자산관리공사 프로젝트 보고서입니다.")
    assert nfd_body != unicodedata.normalize("NFC", nfd_body)  # sanity: really decomposed

    doc_path = tmp_path / "doc.txt"
    doc_path.write_text(nfd_body, encoding="utf-8")

    collector = LocalFileCollector(root=doc_path)
    registry = default_registry()
    chunker = AdaptiveChunker(max_tokens_per_chunk=4000)
    extractor = _CapturingExtractor()

    outcomes = [outcome async for outcome in run_pipeline(collector, registry, chunker, extractor)]

    assert len(outcomes) == 1
    assert extractor.seen_chunk_texts  # the extractor was actually called
    for text in extractor.seen_chunk_texts:
        assert text == unicodedata.normalize("NFC", text)
