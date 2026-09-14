"""골든셋 모델 + 로드/저장 + 코퍼스 지문.

골든셋은 생성 시 grain(`chunk` | `knowledge_object`)을 정하고, 평가는 그 grain으로만 검색·채점한다.
정답 라벨은 kind와 grain에 따라 하나만 쓴다:

- lookup + chunk: `evidence` [{document_id, quote}] - 청크 id가 아니라 인용문이라 재청킹 뒤에도 실행
  시점에 "그 문서에서 quote를 담은 청크"로 다시 해석된다(`resolve_gold`).
- lookup + knowledge_object: `knowledge_object_ids` - 재추출하면 id가 바뀌어 stale이 된다.
- exhaustive: `predicate_sql`(정답 문서 집합을 내는 SELECT) + 생성 당시 결과 `document_ids`.
- none: 라벨 없음(검색 결과가 비어야 정답).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from knowledge_forge.config import settings
from knowledge_forge.search.retrieve import _sql_literal
from knowledge_forge.storage import PipelineStore
from knowledge_forge.text_normalize import to_nfc

Grain = Literal["chunk", "knowledge_object"]
Kind = Literal["lookup", "exhaustive", "none"]


class Evidence(BaseModel):
    document_id: str
    quote: str


class GoldenItem(BaseModel):
    id: str
    query: str
    answer: str = ""
    kind: Kind
    split: Literal["dev", "test"]
    status: Literal["draft", "approved"] = "draft"
    evidence: list[Evidence] = Field(default_factory=list)
    knowledge_object_ids: list[str] = Field(default_factory=list)
    predicate_sql: str | None = None
    document_ids: list[str] = Field(default_factory=list)


class GoldenSet(BaseModel):
    version: str
    grain: Grain
    created_at: datetime
    corpus: dict[str, Any]
    items: list[GoldenItem]


def load_golden(path: str | Path) -> GoldenSet:
    return GoldenSet.model_validate_json(Path(path).read_text(encoding="utf-8"))


def save_golden(golden: GoldenSet, path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(golden.model_dump_json(indent=2), encoding="utf-8")


def normalize_text(text: str) -> str:
    """NFC + 공백 접기 - 인용문 대조와 원문 복사 게이트가 같은 기준을 쓴다."""
    return " ".join(to_nfc(text).split())


def quote_in(text: str, quote: str) -> bool:
    return bool(quote.strip()) and normalize_text(quote) in normalize_text(text)


def ranked_ids(hits: list[dict[str, Any]], grain: str, *, lift_to_documents: bool = False) -> list[str]:
    """검색 히트에서 채점 리스트를 만든다: 그 grain id(`chunk_id` / `knowledge_object_id`)를 가진 히트만
    순서대로, 중복 제거. 제목·필터 히트는 순위에는 참여했지만 grain id가 없어 빠진다. exhaustive는
    `lift_to_documents`로 같은 히트를 소속 문서 id로 올린다."""
    key = "chunk_id" if grain == "chunk" else "knowledge_object_id"
    return list(dict.fromkeys(hit["document_id"] if lift_to_documents else hit[key] for hit in hits if hit.get(key)))


ALL_ROWS = 1_000_000

_FINGERPRINT_SQL = """
SELECT 'documents', COUNT(*), md5(COALESCE(string_agg(d.document_id || ':' || s.content_hash || ':' || COALESCE(d.title, ''), E'\\n' ORDER BY d.document_id), ''))
  FROM documents d JOIN sources s ON s.source_id = d.source_id
UNION ALL
SELECT 'chunks', COUNT(*), md5(COALESCE(string_agg(chunk_id, ',' ORDER BY chunk_id), '')) FROM chunks
UNION ALL
SELECT 'knowledge_objects', COUNT(*), md5(COALESCE(string_agg(knowledge_object_id, ',' ORDER BY knowledge_object_id), ''))
  FROM knowledge_objects
UNION ALL
SELECT 'embeddings:' || target_kind, COUNT(*), NULL FROM knowledge_object_embeddings
  WHERE embedding_model = {model} GROUP BY target_kind
"""


async def corpus_fingerprint(store: PipelineStore) -> dict[str, Any]:
    """문서(id:content_hash:title)·청크 id·KO id의 개수와 해시, 활성 임베딩 모델의 grain별 임베딩 수.
    compare는 `documents`가 다르면 거부하고 나머지가 다르면 경고만 한다."""
    rows, _ = await store.execute_readonly_query(
        _FINGERPRINT_SQL.format(model=_sql_literal(settings.embedding_model)), ALL_ROWS
    )
    fingerprint: dict[str, Any] = {"embeddings": {"chunk": 0, "knowledge_object": 0}}
    for name, count, digest in rows:
        if name.startswith("embeddings:"):
            fingerprint["embeddings"][name.split(":", 1)[1]] = int(count)
        else:
            fingerprint[name] = {"count": int(count), "hash": digest}
    return fingerprint


async def resolve_gold(item: GoldenItem, grain: str, store: PipelineStore) -> tuple[set[str] | None, str | None]:
    """(정답 id 집합, stale 사유). exhaustive는 문서 id, lookup은 grain id, none은 빈 집합."""
    if item.kind == "none":
        return set(), None
    if item.kind == "exhaustive":
        rows, _ = await store.execute_readonly_query(item.predicate_sql or "", ALL_ROWS)
        current = {row[0] for row in rows}
        return (current, None) if current == set(item.document_ids) else (None, "predicate_changed")
    if grain == "knowledge_object":
        ids = set(item.knowledge_object_ids)
        rows, _ = await store.execute_readonly_query(
            f"SELECT knowledge_object_id FROM knowledge_objects WHERE knowledge_object_id IN ({', '.join(map(_sql_literal, sorted(ids)))})",
            ALL_ROWS,
        )
        return (ids, None) if ids and {row[0] for row in rows} == ids else (None, "knowledge_object_missing")
    gold: set[str] = set()
    for evidence in item.evidence:
        chunks = await store.list_chunks_by_document(evidence.document_id)
        matched = {chunk.chunk_id for chunk in chunks if quote_in(chunk.text, evidence.quote)}
        if not matched:
            return None, "quote_not_found"
        gold |= matched
    return (gold, None) if gold else (None, "no_labels")
