"""Document expansion - `read_document`, the whole-document counterpart of
`vector_search`/`file_search` (wiki/search-architecture.md "⑤ Map-Reduce /
Hierarchical Retrieval" → "Document Expansion").

top-k chunk retrieval cannot answer "2025_SH_업무_보고서 요약해줘", "보고서
전체에서 핵심 리스크만 정리", "회의에서 처음 제안된 내용과 최종 결정 비교"
or "A 프로젝트에서 언급되지 않은 기술은?" - a summary, a long-range
extraction, a sequence comparison and an absence check all need *every*
chunk of the document in reading order, not the k that happened to score
highest. This module returns exactly that: one document's chunks ordered
by `sequence_index`, paginated by a character budget so a single
tool_result never swallows the agent's context (the same concern
`storage/postgres_store.py::_CHUNK_HIT_SUMMARY_CHARS` and
`agent/tools.py::_MAX_SEARCH_LIMIT` address for search hits).

Same layering rule as `search/service.py`: a plain async function that
imports nothing from FastAPI/MCP/the agent harness, so the agent tool
(`agent/tools.py::_make_read_document_tool`) and the HTTP route
(`GET /documents/{document_id}/chunks`) are both thin adapters over it.
`store` is a Protocol rather than `PipelineStore` because the HTTP route
only holds a `PostgresStore` (api/deps.py::get_read_store) - both satisfy it.
"""

from __future__ import annotations

from typing import Any, Protocol

from knowledge_forge.models.chunk import Chunk, HierarchyLevel
from knowledge_forge.models.document import Document
from knowledge_forge.models.source import Source


class DocumentReadStore(Protocol):
    async def get_document(self, document_id: str) -> Document | None: ...

    async def get_source(self, source_id: str) -> Source | None: ...

    async def list_chunks_by_document(
        self, document_id: str, *, hierarchy_level: HierarchyLevel | None = None
    ) -> list[Chunk]: ...


def _chunk_to_dict(chunk: Chunk) -> dict[str, Any]:
    page_start, page_end = chunk.page_range if chunk.page_range else (None, None)
    return {
        "chunk_id": chunk.chunk_id,
        "sequence_index": chunk.sequence_index,
        "page_start": page_start,
        "page_end": page_end,
        "text": chunk.text,
    }


async def read_document(
    document_id: str,
    store: DocumentReadStore,
    *,
    max_chars: int | None = None,
    offset: int = 0,
) -> dict[str, Any] | None:
    """문서 하나의 청크 전체를 `sequence_index` 순서로 돌려준다 - 문서 전체 요약/문서 내부
    질문/부재 확인/순서 비교처럼 top-k 검색으로는 답할 수 없는 질의 유형을 위한 서비스 함수.

    반환 모양: `{document_id, title, source: {source_id, source_type, uri, modified_at},
    total_chunks, total_tokens, offset, chunks: [{chunk_id, sequence_index, page_start,
    page_end, text}], truncated, next_offset}`. `document_id`가 없으면 None(호출자가 not-found를
    자기 계층의 방식 - 도구는 에러 결과, HTTP는 404 - 으로 옮긴다).

    `title`은 `documents.metadata.title`만 본다(파서가 못 뽑았으면 None) - 별도 `documents.title`
    컬럼이 있는지는 가정하지 않는다. `source.modified_at`은 `sources` 행에서 가져오며 ISO 문자열이다
    (도구 결과가 `json.dumps`를 거치므로 datetime을 그대로 싣지 않는다).

    `max_chars`는 한 페이지에 실을 본문 글자 수 예산이고 `offset`은 청크 인덱스다(글자 오프셋이
    아님) - 예산이 차면 `truncated=True`와 다음 청크 인덱스 `next_offset`을 돌려주므로, 호출자는
    그 값을 `offset`으로 넘겨 이어 읽으면 된다. 청크는 절대 중간에서 자르지 않는다(자르면 그
    뒷부분을 다시 읽을 방법이 없다). 그래서 **페이지의 첫 청크는 예산을 넘어도 항상 포함**한다 -
    청크 하나가 `max_chars`보다 길 때 빈 페이지 + 같은 `next_offset`을 돌려주면 호출자가 영원히
    제자리걸음이기 때문이다(청크 상한은 `chunking/adaptive.py`의 토큰 예산이므로 초과 폭은
    유한하다). `max_chars=None`이면 예산 없이 전부 돌려준다.

    `total_tokens`는 청크 `token_count`의 합 - 호출자가 "이 문서를 다 읽으려면 페이지가 몇 번
    필요한가"를 가늠하는 용도(`chunking/tokenizer.py`의 cl100k 근사값이라는 한계는 그대로).
    청크를 매 페이지 전부 읽고 서비스에서 자르는 이유: 현재 규모(개발 DB 최대 437청크/21K자)에서
    한 번의 조회가 충분히 싸고, 페이지마다 총계를 다시 세는 별도 쿼리를 두는 것보다 단순하다."""
    if max_chars is not None and max_chars < 1:
        raise ValueError(f"max_chars must be positive, got {max_chars}")

    document = await store.get_document(document_id)
    if document is None:
        return None
    source = await store.get_source(document.source.source_id)
    chunks = await store.list_chunks_by_document(document_id)

    total_chunks = len(chunks)
    start = max(0, offset)
    page: list[dict[str, Any]] = []
    used = 0
    index = start
    while index < total_chunks:
        text_len = len(chunks[index].text)
        if max_chars is not None and page and used + text_len > max_chars:
            break
        page.append(_chunk_to_dict(chunks[index]))
        used += text_len
        index += 1
    truncated = index < total_chunks

    modified_at = source.modified_at if source is not None else None
    return {
        "document_id": document.document_id,
        "title": document.metadata.title,
        "source": {
            "source_id": document.source.source_id,
            "source_type": document.source.source_type.value,
            "uri": document.source.uri,
            "modified_at": modified_at.isoformat() if modified_at is not None else None,
        },
        "total_chunks": total_chunks,
        "total_tokens": sum(chunk.token_count for chunk in chunks),
        "offset": start,
        "chunks": page,
        "truncated": truncated,
        "next_offset": index if truncated else None,
    }
