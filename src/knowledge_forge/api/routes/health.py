"""Liveness endpoint - no DB dependency, so it stays green for the
container healthcheck even if Postgres is briefly unreachable."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}
