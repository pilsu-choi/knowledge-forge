"""`EmbeddingProvider.embed()`'s request-size budgeting (embedding/base.py).

Regression coverage for the reparse bug where `storage/store.py`'s
`record_chunk_embeddings`/`record_embeddings` sent an entire document's
chunks - thousands, for a large PDF - in one `embed()` call, and OpenRouter
rejected it with "maximum request size is 300000 tokens per request". The
fix batches inside `EmbeddingProvider.embed()` itself so both callers get it
for free and no caller has to re-derive a safe request size.
"""

from knowledge_forge.embedding.base import EmbeddingProvider, _batch_by_budget


class _RecordingProvider(EmbeddingProvider):
    """Counts calls and their sizes instead of hitting a real API - one
    fixed-length vector per input, order preserved, so callers can verify
    `embed()`'s output lines up with `texts` across however many batches it
    took."""

    def __init__(self):
        self.batches: list[list[str]] = []

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        return [[float(len(t))] for t in texts]


async def test_embed_is_one_request_when_well_under_budget():
    provider = _RecordingProvider()

    vectors = await provider.embed(["a", "b", "c"])

    assert len(provider.batches) == 1
    assert vectors == [[1.0], [1.0], [1.0]]


async def test_embed_splits_into_multiple_requests_over_the_token_budget():
    provider = _RecordingProvider()
    # "word" is one cl100k_base token, so 5 * 60,000 =~ 300,000 tokens -
    # comfortably over the module's 250,000-token default request budget,
    # without depending on tiktoken's exact count for a smaller input.
    texts = ["word " * 60_000 for _ in range(5)]

    vectors = await provider.embed(texts)

    assert len(vectors) == 5
    assert sum(len(b) for b in provider.batches) == 5  # every text sent exactly once
    assert len(provider.batches) > 1  # and it took more than one request to do it


async def test_embed_preserves_input_order_across_batches():
    provider = _RecordingProvider()
    texts = [f"text-{i}" for i in range(50)]

    vectors = await provider.embed(texts)

    assert vectors == [[float(len(t))] for t in texts]


async def test_embed_of_empty_list_makes_no_request():
    provider = _RecordingProvider()

    assert await provider.embed([]) == []
    assert provider.batches == []


def test_batch_by_budget_respects_a_small_token_budget():
    texts = ["hello world"] * 10  # 2 tokens each under cl100k_base

    batches = _batch_by_budget(texts, max_tokens_per_item=100, max_tokens_per_request=5, max_items_per_request=100)

    assert [t for batch in batches for t in batch] == texts  # nothing lost or reordered
    assert all(len(batch) <= 2 for batch in batches)  # 5-token budget / 2 tokens per item


def test_batch_by_budget_respects_a_small_item_count():
    texts = ["x"] * 10

    batches = _batch_by_budget(
        texts, max_tokens_per_item=100, max_tokens_per_request=1_000_000, max_items_per_request=3
    )

    assert [len(b) for b in batches] == [3, 3, 3, 1]


def test_batch_by_budget_sends_a_lone_oversized_item_by_itself():
    """A single text over the request budget on its own can't be split
    further here (nothing shortens someone else's text) - it still has to go
    out, alone, and let the provider decide."""
    huge = "word " * 100_000
    texts = ["small before", huge, "small after"]

    batches = _batch_by_budget(texts, max_tokens_per_item=8191, max_tokens_per_request=1000, max_items_per_request=2048)

    assert batches[1] == [huge]
    assert [t for batch in batches for t in batch] == texts
