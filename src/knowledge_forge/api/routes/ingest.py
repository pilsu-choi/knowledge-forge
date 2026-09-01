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

router = APIRouter(tags=["Ingest"])


@router.post(
    "/ingest",
    response_model=IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Enqueue a local file ingest job",
    description="로컬 경로를 확인한 뒤 `local_file_ingest` job을 enqueue하고 즉시 202를 반환한다. "
    "실제 처리는 별도 worker가 수행한다.",
)
async def ingest(request: IngestRequest, job_store: JobStore = Depends(get_job_store)) -> IngestResponse:
    if not Path(request.path).exists():
        raise HTTPException(status_code=400, detail=f"path not found: {request.path}")

    job = await job_store.enqueue(
        "local_file_ingest",
        {"path": request.path, "max_tokens": settings.max_tokens_per_chunk, "model": None},
    )
    return IngestResponse(job_id=job.job_id, status=job.status)
