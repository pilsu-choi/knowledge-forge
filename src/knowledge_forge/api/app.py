"""FastAPI app factory. `knowledge-forge serve` (cli.py) runs this under
uvicorn; see wiki/api.md for the endpoint list and job lifecycle.
"""

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from knowledge_forge.api.routes import health, ingest, jobs, knowledge, sources
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


_TAGS_METADATA = [
    {"name": "Health", "description": "liveness 체크. DB 의존 없음(컨테이너 healthcheck용)."},
    {"name": "Ingest", "description": "로컬 파일 또는 SaaS 소스를 job으로 enqueue한다 (`local_file_ingest`/`{source}_ingest`)."},
    {"name": "Jobs", "description": "enqueue된 job의 상태/결과 조회."},
    {"name": "Sources", "description": "수집된 원본(Source) 조회."},
    {"name": "Documents", "description": "파싱된 Document(elements 포함) 조회."},
    {"name": "Knowledge Objects", "description": "추출된 KnowledgeObject 조회."},
    {"name": "Relations", "description": "KnowledgeObject 간 Relation 조회."},
]


def create_app() -> FastAPI:
    app = FastAPI(
        title="Knowledge Forge API",
        description="Ingest/Process 분리 구조의 job queue API. 자세한 내용은 wiki/api.md 참고.",
        version="0.1.0",
        openapi_tags=_TAGS_METADATA,
        lifespan=_lifespan,
    )
    app.include_router(health.router)
    app.include_router(ingest.router)
    app.include_router(jobs.router)
    app.include_router(sources.router)
    app.include_router(knowledge.router)
    return app
