"""FastAPI app factory. `knowledge-forge serve` (cli.py) runs this under
uvicorn; see wiki/api.md for the endpoint list and job lifecycle.
"""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Scope

from knowledge_forge.api.routes import health, ingest, jobs, knowledge, okf, sources
from knowledge_forge.config import settings
from knowledge_forge.scheduling.auto_enqueue import parse_scheduled_sources, start_auto_enqueue_scheduler
from knowledge_forge.storage.job_store import JobStore


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Best-effort, once, at startup - not per request (api/deps.py). Skipped
    # entirely when no KF_DATABASE_URL is set so the server can still come
    # up and serve /health (every other route 503s via api/deps.py).
    scheduler = None
    if settings.database_url:
        async with JobStore(settings.database_url) as store:
            await store.init_schema()

        # Auto-scheduling (KF_SCHEDULED_SOURCES, scheduling/auto_enqueue.py):
        # starting `serve` is enough to also start periodic sync, with no
        # separate `knowledge-forge schedule` process. Disabled by default -
        # only starts when KF_SCHEDULED_SOURCES is actually configured.
        # Parsing before the try-less call is deliberate: a malformed value
        # should fail server startup loudly, not silently schedule nothing.
        scheduled_sources = parse_scheduled_sources(settings.scheduled_sources)
        if scheduled_sources:
            scheduler = start_auto_enqueue_scheduler(settings.database_url, scheduled_sources)
    yield
    if scheduler is not None:
        scheduler.shutdown()


_TAGS_METADATA = [
    {"name": "Health", "description": "liveness 체크. DB 의존 없음(컨테이너 healthcheck용)."},
    {"name": "Ingest", "description": "로컬 파일 또는 SaaS 소스를 job으로 enqueue한다 (`local_file_ingest`/`{source}_ingest`)."},
    {"name": "Jobs", "description": "enqueue된 job의 상태/결과 조회."},
    {"name": "Sources", "description": "수집된 원본(Source) 조회."},
    {"name": "Documents", "description": "파싱된 Document(elements 포함) 조회."},
    {"name": "Knowledge Objects", "description": "추출된 KnowledgeObject 조회."},
    {"name": "Relations", "description": "KnowledgeObject 간 Relation 조회."},
    {"name": "OKF", "description": "OKF 파일 트리(KF_OKF_DIR) 조회. Postgres 불필요 - /ui가 이 API로 뜬다."},
]

# React+Vite app in frontend/ (frontend/vite.config.ts builds with
# base:'/ui/' to match this mount). Built by `npm --prefix frontend run
# build` - not committed (frontend/dist is gitignored, same as any dist/).
_FRONTEND_DIST = Path(__file__).parent.parent.parent.parent / "frontend" / "dist"


class _SPAStaticFiles(StaticFiles):
    """Client-side routing (react-router) means most `/ui/<project>/...`
    URLs don't correspond to a real file on disk - only `index.html` and the
    hashed `assets/*` bundle do. Falls back to `index.html` for anything else
    instead of 404ing, so a browser refresh or a pasted deep link works."""

    async def get_response(self, path: str, scope: Scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404:
                return await super().get_response("index.html", scope)
            raise


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
    app.include_router(okf.router)
    # StaticFiles() raises at construction if the directory is missing, which
    # would otherwise take down the whole app (including /health) on a fresh
    # clone that hasn't run the frontend build yet - skip the mount instead.
    if _FRONTEND_DIST.is_dir():
        app.mount("/ui", _SPAStaticFiles(directory=_FRONTEND_DIST, html=True), name="ui")
    return app
