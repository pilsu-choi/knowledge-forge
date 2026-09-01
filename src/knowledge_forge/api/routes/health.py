"""Liveness endpoint - no DB dependency, so it stays green for the
container healthcheck even if Postgres is briefly unreachable."""

from fastapi import APIRouter

router = APIRouter(tags=["Health"])


@router.get("/health", summary="Liveness check")
async def health() -> dict:
    return {"status": "ok"}
