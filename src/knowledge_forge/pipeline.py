"""Orchestrates Collector -> Parser -> Adaptive Chunker -> Knowledge Extraction.

This is the one place that wires the stage interfaces together; each stage
stays swappable (a different Collector, Parser registry, Chunker, or
Extractor implementation) without touching this module's logic.
"""

import logging
from datetime import datetime, timezone
from typing import Iterator

from knowledge_forge.chunking.base import Chunker
from knowledge_forge.collectors.base import Collector, RawItem
from knowledge_forge.exceptions import KnowledgeExtractionError
from knowledge_forge.extraction.base import Extractor
from knowledge_forge.ids import stable_id
from knowledge_forge.models.knowledge_object import KnowledgeObject
from knowledge_forge.models.source import Source, SourceRef
from knowledge_forge.parsers.registry import ParserRegistry

logger = logging.getLogger(__name__)


def _make_source(collector: Collector, item: RawItem) -> Source:
    return Source(
        source_id=stable_id(collector.source_type.value, item.uri),
        source_type=collector.source_type,
        uri=item.uri,
        content_hash=item.content_hash,
        collected_at=datetime.now(timezone.utc),
        modified_at=item.modified_at,
        metadata=item.metadata,
    )


def run_pipeline(
    collector: Collector,
    parser_registry: ParserRegistry,
    chunker: Chunker,
    extractor: Extractor,
    cursor=None,
) -> Iterator[KnowledgeObject]:
    """Run the full pipeline over everything a Collector yields.

    Items a Parser doesn't support (UnsupportedFormatError/NotImplementedError)
    or that fail extraction (KnowledgeExtractionError) are logged and skipped
    rather than aborting the whole run - one bad file shouldn't block the rest
    of a batch.
    """
    for item in collector.sync(cursor):
        if item.deleted:
            logger.info("skipping deleted item: %s", item.uri)
            continue

        source = _make_source(collector, item)
        source_ref = SourceRef.from_source(source)

        try:
            parser = parser_registry.get_parser_for(item.uri)
            document = parser.parse(item, source_ref)
        except NotImplementedError as exc:
            logger.warning("no working parser for %s, skipping: %s", item.uri, exc)
            continue
        except Exception as exc:  # noqa: BLE001 - a bad file must not abort the run
            logger.error("failed to parse %s, skipping: %s", item.uri, exc)
            continue

        chunks = chunker.chunk(document)
        for chunk in chunks:
            try:
                yield extractor.extract(chunk, document, source)
            except KnowledgeExtractionError as exc:
                logger.error("extraction failed for chunk %s, skipping: %s", chunk.chunk_id, exc)
                continue
