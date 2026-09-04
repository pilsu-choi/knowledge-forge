"""GET /sources, GET /documents - read-only listing/lookup for the two
provenance layers every downstream KnowledgeObject traces back to
(wiki/storage.md). `project_id` is the tenancy filter (wiki/api.md
"Multi-tenant") - every row carries one (default "default" for
single-tenant callers), so a UI can list only the sources/documents that
belong to the caller's project.
"""


from fastapi import APIRouter, Depends, HTTPException

from knowledge_forge.api.deps import get_read_store
from knowledge_forge.api.schemas import DocumentResponse, Page, SourceResponse
from knowledge_forge.models.document import Document
from knowledge_forge.models.source import Source
from knowledge_forge.storage.postgres_store import PostgresStore

router = APIRouter()


def _source_to_response(source: Source) -> SourceResponse:
    return SourceResponse(
        source_id=source.source_id,
        source_type=source.source_type.value,
        uri=source.uri,
        content_hash=source.content_hash,
        collected_at=source.collected_at,
        modified_at=source.modified_at,
        metadata=source.metadata,
        project_id=source.project_id,
    )


def _document_to_response(document: Document) -> DocumentResponse:
    return DocumentResponse(
        document_id=document.document_id,
        source_id=document.source.source_id,
        project_id=document.source.project_id,
        metadata=document.metadata.model_dump(mode="json"),
        elements=[e.model_dump(mode="json") for e in document.elements],
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


@router.get("/sources", response_model=Page[SourceResponse], tags=["Sources"], summary="List sources")
async def list_sources(
    project_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    store: PostgresStore = Depends(get_read_store),
) -> Page[SourceResponse]:
    sources = await store.list_sources(project_id=project_id, limit=limit, offset=offset)
    return Page(items=[_source_to_response(s) for s in sources], limit=limit, offset=offset)


@router.get(
    "/sources/{source_id}",
    response_model=SourceResponse,
    tags=["Sources"],
    summary="Get a source by id",
    responses={404: {"description": "source not found"}},
)
async def get_source(source_id: str, store: PostgresStore = Depends(get_read_store)) -> SourceResponse:
    source = await store.get_source(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail=f"source not found: {source_id}")
    return _source_to_response(source)


@router.get("/documents", response_model=Page[DocumentResponse], tags=["Documents"], summary="List documents")
async def list_documents(
    project_id: str | None = None,
    source_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    store: PostgresStore = Depends(get_read_store),
) -> Page[DocumentResponse]:
    documents = await store.list_documents(project_id=project_id, source_id=source_id, limit=limit, offset=offset)
    return Page(items=[_document_to_response(d) for d in documents], limit=limit, offset=offset)


@router.get(
    "/documents/{document_id}",
    response_model=DocumentResponse,
    tags=["Documents"],
    summary="Get a document by id",
    responses={404: {"description": "document not found"}},
)
async def get_document(document_id: str, store: PostgresStore = Depends(get_read_store)) -> DocumentResponse:
    document = await store.get_document(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail=f"document not found: {document_id}")
    return _document_to_response(document)
