"""Deterministic id helpers shared across the pipeline.

Ids are content/lineage derived (not random uuids) so re-running the
pipeline on unchanged input reproduces the same ids - useful for
incremental sync and idempotent re-extraction.
"""

import hashlib


def stable_id(*parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return digest[:32]


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
