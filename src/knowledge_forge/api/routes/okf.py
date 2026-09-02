"""GET /okf/* - read-only browser for an OKF file tree (wiki/okf.md), used by
the static viewer at /ui (api/app.py). Reads straight off disk via
okf/reader.py - no Postgres, unlike every other router in this package.
"""

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from knowledge_forge.api.deps import get_okf_root
from knowledge_forge.okf import reader

router = APIRouter(prefix="/okf", tags=["OKF"])


def _project_root(okf_root: Path, project_id: str) -> Path:
    if project_id not in reader.list_projects(okf_root):
        raise HTTPException(status_code=404, detail=f"OKF project not found: {project_id}")
    return okf_root / project_id


@router.get("/projects", summary="List OKF projects")
async def list_projects(okf_root: Path = Depends(get_okf_root)) -> list[str]:
    return reader.list_projects(okf_root)


@router.get(
    "/projects/{project_id}/manifest",
    summary="Get a project's manifest.yaml (id/type/path catalog)",
    responses={404: {"description": "project not found"}},
)
async def get_manifest(project_id: str, okf_root: Path = Depends(get_okf_root)) -> dict[str, Any]:
    project_root = _project_root(okf_root, project_id)
    return reader.read_manifest(project_root)


@router.get(
    "/projects/{project_id}/objects/{type}",
    summary="List a project's KnowledgeObjects of one type",
    responses={404: {"description": "project not found"}},
)
async def list_objects(project_id: str, type: str, okf_root: Path = Depends(get_okf_root)) -> list[dict[str, Any]]:
    project_root = _project_root(okf_root, project_id)
    manifest = reader.read_manifest(project_root)
    return reader.list_objects_by_type(project_root, manifest, type)


@router.get(
    "/projects/{project_id}/graph",
    summary="Get a project's relation graph (documents/objects/edges, Cytoscape-ready)",
    responses={404: {"description": "project not found"}},
)
async def get_graph(project_id: str, okf_root: Path = Depends(get_okf_root)) -> dict[str, Any]:
    project_root = _project_root(okf_root, project_id)
    manifest = reader.read_manifest(project_root)
    return reader.build_graph(project_root, manifest)


@router.get(
    "/projects/{project_id}/file/{rel_path:path}",
    summary="Read one markdown file (object/resource/log.md/views/index.md) with rendered body",
    responses={404: {"description": "project or file not found"}, 400: {"description": "invalid rel_path"}},
)
async def get_file(project_id: str, rel_path: str, okf_root: Path = Depends(get_okf_root)) -> dict[str, Any]:
    project_root = _project_root(okf_root, project_id)
    try:
        return reader.read_markdown_file(project_root, rel_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"file not found: {exc}") from exc
