"""POST /ingest - the "respond immediately, process in the background"
half of Ingest/Process separation (wiki/roadmap.md 4단계). `source`
("local", the default, or one of collectors.saas.factory.SAAS_COLLECTOR_BUILDERS)
picks the job_type enqueued - "local_file_ingest" for local, or
"{source}_ingest" (e.g. "slack_ingest") for a SaaS source - mirroring
cli.py extract/schedule's --source. Writes the `jobs` row, then hands its
job_id to Celery (celery_app.run_ingest_job) over Redis so a
`knowledge-forge worker` process actually runs it - worker.py's _dispatch()
is what builds the matching Collector once it does.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status

from knowledge_forge.api.deps import get_job_store
from knowledge_forge.api.schemas import IngestRequest, IngestResponse
from knowledge_forge.celery_app import run_ingest_job
from knowledge_forge.collectors.base import ensure_utc
from knowledge_forge.collectors.saas.factory import SAAS_COLLECTOR_BUILDERS, job_type_for_source
from knowledge_forge.config import settings
from knowledge_forge.storage.job_store import JobStore

router = APIRouter(tags=["Ingest"])


def _backfill_payload(request: IngestRequest) -> dict:
    """`since`/`until` round-trip through the job payload as ISO strings
    (payload must be JSON-safe, see JobStore) - worker.py's `_dispatch`
    parses them back with `datetime.fromisoformat`. Naive values are
    normalized to UTC here (see `ensure_utc`) so that round-trip and
    `Collector.backfill_sync`'s comparisons against UTC-aware
    `RawItem.modified_at` are both unambiguous.
    """
    if request.since is None:
        return {}
    since = ensure_utc(request.since)
    until = ensure_utc(request.until) if request.until is not None else None
    if until is not None and since > until:
        raise HTTPException(status_code=400, detail="since must be <= until")
    payload: dict = {"since": since.isoformat()}
    if until is not None:
        payload["until"] = until.isoformat()
    return payload


@router.post(
    "/ingest",
    response_model=IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Enqueue an ingest job",
    description="`source`(기본값 local)에 맞는 job(`local_file_ingest` 또는 `{source}_ingest`)을 "
    "enqueue하고 즉시 202를 반환한다. 실제 처리는 별도 worker가 수행한다. `since`(옵션, `until`과 함께)를 "
    "주면 증분 동기화 커서와 무관하게 해당 구간만 재수집하는 backfill job이 된다 (wiki/scheduling.md 참고).",
)
async def ingest(request: IngestRequest, job_store: JobStore = Depends(get_job_store)) -> IngestResponse:
    max_tokens = request.max_tokens or settings.max_tokens_per_chunk
    backfill_payload = _backfill_payload(request)

    if request.source == "local":
        if not request.path:
            raise HTTPException(status_code=400, detail="path is required when source is 'local'")
        if not Path(request.path).exists():
            raise HTTPException(status_code=400, detail=f"path not found: {request.path}")

        job = await job_store.enqueue(
            job_type_for_source("local"),
            {
                "path": request.path,
                "max_tokens": max_tokens,
                "model": request.model,
                "okf_dir": request.okf_dir,
                **backfill_payload,
            },
            project_id=request.project_id,
        )
        run_ingest_job.apply_async(args=[job.job_id], task_id=job.job_id)
        return IngestResponse(job_id=job.job_id, status=job.status)

    if request.source not in SAAS_COLLECTOR_BUILDERS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown source '{request.source}' (local/{'/'.join(SAAS_COLLECTOR_BUILDERS)} 중 하나여야 합니다)",
        )

    job = await job_store.enqueue(
        job_type_for_source(request.source),
        {"max_tokens": max_tokens, "model": request.model, "okf_dir": request.okf_dir, **backfill_payload},
        project_id=request.project_id,
    )
    run_ingest_job.apply_async(args=[job.job_id], task_id=job.job_id)
    return IngestResponse(job_id=job.job_id, status=job.status)
