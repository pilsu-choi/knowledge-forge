from fastapi import APIRouter, Depends, HTTPException
from typing import Optional

from knowledge_forge.api.deps import get_job_store
from knowledge_forge.api.schemas import JobResponse, Page
from knowledge_forge.storage.job_store import JobStatus, JobStore

router = APIRouter()


def _to_response(job) -> JobResponse:
    return JobResponse(**job.model_dump())


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: str, job_store: JobStore = Depends(get_job_store)) -> JobResponse:
    job = await job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
    return _to_response(job)


@router.get("/jobs", response_model=Page[JobResponse])
async def list_jobs(
    status: Optional[JobStatus] = None,
    limit: int = 50,
    offset: int = 0,
    job_store: JobStore = Depends(get_job_store),
) -> Page[JobResponse]:
    jobs = await job_store.list(status=status, limit=limit, offset=offset)
    return Page(items=[_to_response(j) for j in jobs], limit=limit, offset=offset)
