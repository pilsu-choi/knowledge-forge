"""POST /ingest - the "respond immediately, process in the background"
half of Ingest/Process separation (wiki/roadmap.md 4단계). Only
`local_file_ingest` exists today; a SaaS source becomes a new job_type once
1단계 (CLI/OAuth wiring) lands, not a redesign of this endpoint.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status

from knowledge_forge.api.deps import get_job_store
from knowledge_forge.api.schemas import IngestRequest, IngestResponse
from knowledge_forge.config import settings
from knowledge_forge.storage.job_store import JobStore

router = APIRouter()


@router.post("/ingest", response_model=IngestResponse, status_code=status.HTTP_202_ACCEPTED)
async def ingest(request: IngestRequest, job_store: JobStore = Depends(get_job_store)) -> IngestResponse:
    if not Path(request.path).exists():
        raise HTTPException(status_code=400, detail=f"path not found: {request.path}")

    job = await job_store.enqueue(
        "local_file_ingest",
        {"path": request.path, "max_tokens": settings.max_tokens_per_chunk, "model": None},
    )
    return IngestResponse(job_id=job.job_id, status=job.status)
