"""FastAPI app factory. `knowledge-forge serve` (cli.py) runs this under
uvicorn; see wiki/api.md for the endpoint list and job lifecycle.
"""

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from knowledge_forge.api.routes import health, ingest, jobs, knowledge
from knowledge_forge.config import settings
from knowledge_forge.storage.job_store import JobStore


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Best-effort, once, at startup - not per request (api/deps.py). Skipped
    # entirely when no KF_DATABASE_URL is set so the server can still come
    # up and serve /health (every other route 503s via api/deps.py).
    if settings.database_url:
        async with JobStore(settings.database_url) as store:
            await store.init_schema()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Knowledge Forge API", lifespan=_lifespan)
    app.include_router(health.router)
    app.include_router(ingest.router)
    app.include_router(jobs.router)
    app.include_router(knowledge.router)
    return app
