import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

from knowledge_forge.chunking.tokenizer import count_tokens

logger = logging.getLogger(__name__)

EmbeddingTargetKind = Literal["knowledge_object", "chunk"]

# Conservative request-shape limits shared by every provider - reparse hit
# OpenRouter's "maximum request size is 300000 tokens per request" 400 by
# sending an entire document's chunks (a large PDF: thousands of them) in
# one `embed()` call (storage/store.py's `record_chunk_embeddings`/
# `record_embeddings` never split). 250k leaves headroom for cl100k_base
# (this repo's tokenizer, see chunking/tokenizer.py) undercounting a
# provider's own tokenizer, which is only ever approximate across vendors.
# 8,191 matches OpenAI's per-input limit for the text-embedding-3-* family;
# 2,048 matches its per-request item cap - both apply beyond OpenAI too
# since OpenRouter's own limits are unpublished and these are strictly the
# tighter of the two known ceilings.
_MAX_TOKENS_PER_ITEM = 8191
_MAX_TOKENS_PER_REQUEST = 250_000
_MAX_ITEMS_PER_REQUEST = 2048


def _batch_by_budget(
    texts: list[str],
    max_tokens_per_item: int = _MAX_TOKENS_PER_ITEM,
    max_tokens_per_request: int = _MAX_TOKENS_PER_REQUEST,
    max_items_per_request: int = _MAX_ITEMS_PER_REQUEST,
) -> list[list[str]]:
    """Groups `texts` into request-sized batches so `EmbeddingProvider.embed()`
    never asks a provider for more than it accepts in one call. Greedy and
    order-preserving (texts stay in their original order, just split into
    consecutive runs) - callers zip the returned vectors back against their
    inputs positionally, so reordering would silently mis-attach an
    embedding to the wrong Chunk/KnowledgeObject.

    A single text whose own token count already exceeds `max_tokens_per_item`
    is not split or truncated (nothing here knows how to shorten someone
    else's text without changing what gets embedded) - it is still sent,
    alone, as its own one-item batch, so the *provider* decides whether that
    one input is too large rather than being silently dropped here.
    """
    batches: list[list[str]] = []
    current: list[str] = []
    current_tokens = 0
    for text in texts:
        tokens = count_tokens(text)
        if tokens > max_tokens_per_item:
            logger.warning(
                "one embedding input is %d tokens, over the %d-token per-input budget - the provider may reject it",
                tokens,
                max_tokens_per_item,
            )
        would_overflow = current and (
            current_tokens + tokens > max_tokens_per_request or len(current) >= max_items_per_request
        )
        if would_overflow:
            batches.append(current)
            current, current_tokens = [], 0
        current.append(text)
        current_tokens += tokens
    if current:
        batches.append(current)
    return batches


@dataclass
class EmbeddingRecord:
    """Corresponds to one row of the `knowledge_object_embeddings` table -
    `PostgresStore` upserts this shape as-is (see storage/postgres_store.py's
    `save_embeddings()` and storage/store.py's `record_embeddings()` /
    `record_chunk_embeddings()`, which expect exactly these field names).
    Do not rename these fields.

    Exactly one of `knowledge_object_id` / `chunk_id` is set, matching
    `target_kind` (the table's CHECK constraint enforces the same XOR).
    `document_id` is always set - it is the identifier both grains share,
    see schema.sql's comment on the table."""

    target_kind: EmbeddingTargetKind
    document_id: str
    embedding_model: str
    embedding: list[float]
    embedded_text: str
    embedded_text_hash: str
    project_id: str
    knowledge_object_id: str | None = None
    chunk_id: str | None = None

    def __post_init__(self) -> None:
        if self.target_kind == "knowledge_object":
            if not self.knowledge_object_id or self.chunk_id is not None:
                raise ValueError("target_kind='knowledge_object' requires knowledge_object_id and no chunk_id")
        elif self.target_kind == "chunk":
            if not self.chunk_id or self.knowledge_object_id is not None:
                raise ValueError("target_kind='chunk' requires chunk_id and no knowledge_object_id")
        else:
            raise ValueError(f"unknown target_kind: {self.target_kind!r}")

    @property
    def target_id(self) -> str:
        """The embedded object's own id (`knowledge_object_id` or `chunk_id`,
        whichever `target_kind` names)."""
        target = self.knowledge_object_id if self.target_kind == "knowledge_object" else self.chunk_id
        assert target is not None  # guaranteed by __post_init__
        return target

    @property
    def embedding_id(self) -> str:
        """Deterministic surrogate PK for the `knowledge_object_embeddings`
        row - `stable_id(target_kind, target_id, embedding_model)`, so
        re-embedding the same target with the same model upserts in place
        (the old composite-PK behaviour) rather than inserting a duplicate."""
        from knowledge_forge.ids import stable_id

        return stable_id("embedding", self.target_kind, self.target_id, self.embedding_model)


class EmbeddingProvider(ABC):
    """Turns a batch of texts (KnowledgeObject summaries or raw Chunk text -
    see embedding/text.py) into vectors. `embed()` takes a list rather than
    one text at a time because embedding providers price and rate-limit per
    request, not per text - batching many texts into one call (as
    pipeline.py does for a document's merged KnowledgeObjects, or its
    chunks) is both cheaper and faster than one call per object, mirroring
    why Extractor.extract() takes a bundle of Chunks instead of a single
    Chunk.

    `embed()` itself is concrete and final here on purpose: it splits
    `texts` into `_batch_by_budget()`-sized requests and hands each to
    `_embed_batch()` (what a subclass implements), so no caller - and no
    subclass - has to re-derive a safe request size. Before this split
    existed, `storage/store.py`'s `record_chunk_embeddings`/
    `record_embeddings` sent an entire document's chunks in one call, which
    is what produced OpenRouter's "maximum request size is 300000 tokens per
    request" 400 on a large `reparse` (wiki/reingest-replay.md).
    """

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Returns one embedding vector per input text, in the same order as
        `texts` (same length too - every input gets a vector back), no
        matter how many requests it took to fetch them."""
        if not texts:
            return []
        vectors: list[list[float]] = []
        for batch in _batch_by_budget(texts):
            vectors.extend(await self._embed_batch(batch))
        return vectors

    @abstractmethod
    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """One provider request's worth of `texts` - already within this
        module's request-size budget. Same contract as `embed()` (one vector
        per text, same order) for exactly this one batch."""
        ...
