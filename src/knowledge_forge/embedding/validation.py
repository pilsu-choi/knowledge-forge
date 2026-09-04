"""Response-shape checks shared by every EmbeddingProvider implementation.

Unlike the Extractor providers - whose response shapes differ enough that
extraction/openrouter_extractor.py deliberately duplicates ClaudeExtractor's
parsing rather than sharing a base (see its module docstring) - every
embedding provider here speaks the same OpenAI `/embeddings` contract and
returns the exact same `list[list[float]]`. The two things worth checking
about that list are therefore identical for all of them, so they live here
instead of being copy-pasted per provider.
"""

from knowledge_forge.exceptions import EmbeddingError


def ensure_one_vector_per_text(vectors: list[list[float]], texts: list[str], model: str) -> None:
    """`EmbeddingProvider.embed()`'s contract is one vector per input text, in
    input order (embedding/base.py) - and callers rely on it positionally:
    pipeline.py zips the returned vectors against the KnowledgeObjects/Chunks
    it sent. A short response would therefore silently attach vectors to the
    WRONG objects rather than fail, so a length mismatch has to be an error
    here, at the provider boundary.
    """
    if len(vectors) != len(texts):
        raise EmbeddingError(
            f"임베딩 응답 개수 불일치: 입력 {len(texts)}개에 대해 벡터 {len(vectors)}개를 받았습니다"
            f" (모델 '{model}'). 입력과 출력을 위치로 짝지을 수 없어 중단합니다."
        )


def ensure_expected_dimensions(vectors: list[list[float]], model: str, expected_dimensions: int) -> None:
    """Fails fast when the model's output dimension doesn't match
    `KF_EMBEDDING_DIMENSIONS`, which must equal the `vector(N)` width of
    storage/schema.sql's `knowledge_object_embeddings.embedding` column.

    Without this check the mismatch only surfaces at INSERT time as a raw
    psycopg/pgvector "expected N dimensions, not M" error several layers away
    from its cause - and with `KF_EMBEDDING_PROVIDER=openrouter` that is now
    an easy mistake to make: OpenRouter routes to plenty of 3072/4096-dim
    embedding models (openai/text-embedding-3-large, qwen/qwen3-embedding-8b,
    ...), so picking one in KF_EMBEDDING_MODEL breaks the schema's default
    1536 immediately. Only the first vector is measured - one API call
    returns one model's output, so they are all the same width.
    """
    if not vectors:
        return
    actual = len(vectors[0])
    if actual != expected_dimensions:
        raise EmbeddingError(
            f"임베딩 차원 불일치: 모델 '{model}'이 {actual}차원 벡터를 반환했지만"
            f" KF_EMBEDDING_DIMENSIONS는 {expected_dimensions}입니다.\n"
            "  이 값은 storage/schema.sql의 knowledge_object_embeddings.embedding vector(N)과"
            " 반드시 같아야 하므로, 이대로 저장하면 INSERT 시점에 실패합니다.\n"
            f"  {expected_dimensions}차원 모델로 되돌리거나(KF_EMBEDDING_MODEL),"
            f" KF_EMBEDDING_DIMENSIONS와 schema.sql의 vector(N)을 {actual}로 함께 고친 뒤"
            " 기존 임베딩을 재생성하세요."
        )
