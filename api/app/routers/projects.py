"""Projects: ChatGPT/Claude-style grouping of chats.

A project carries a name, custom instructions, and small text knowledge
files. Chats attach via the session payload; deleting a project unfiles its
chats and removes its files.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app.core.logging import log
from app.core.routes import SafeAPIRoute
from app.services import integrations as integration_service
from app.services import projects as project_service
from app.store import repository as repo

router = APIRouter(prefix="/api/projects", tags=["projects"], route_class=SafeAPIRoute)


class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    instructions: str = Field(default="", max_length=8000)


class UpdateProjectRequest(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    instructions: str | None = Field(default=None, max_length=8000)


@router.get("")
def list_projects() -> dict[str, Any]:
    return {"projects": repo.list_projects()}


@router.post("")
def create_project(req: CreateProjectRequest) -> dict[str, Any]:
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "project name required")
    project = repo.save_project(name, req.instructions)
    log.info("project created", project_id=project["id"], name=name)
    return project


@router.get("/{project_id}")
def get_project(project_id: str) -> dict[str, Any]:
    project = repo.get_project(project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    files = repo.list_project_files(project_id)
    threads = [t for t in repo.list_threads() if t.get("projectId") == project_id]
    return {**project, "files": files, "threads": threads}


@router.patch("/{project_id}")
def update_project(project_id: str, req: UpdateProjectRequest) -> dict[str, Any]:
    updated = repo.update_project(project_id, name=req.name, instructions=req.instructions)
    if updated is None:
        raise HTTPException(404, "project not found")
    return updated


@router.delete("/{project_id}", status_code=204)
def delete_project(project_id: str) -> None:
    integration_service.delete_project_repositories(project_id)
    if not repo.delete_project(project_id):
        raise HTTPException(404, "project not found")


@router.get("/{project_id}/files")
def list_files(project_id: str) -> dict[str, Any]:
    if repo.get_project(project_id) is None:
        raise HTTPException(404, "project not found")
    return {"files": repo.list_project_files(project_id)}


@router.post("/{project_id}/files")
async def upload_files(
    project_id: str, files: list[UploadFile] = File(default_factory=list)
) -> dict[str, Any]:
    """Store small text knowledge files (rules, metric definitions, schema docs)."""
    if repo.get_project(project_id) is None:
        raise HTTPException(404, "project not found")
    if not files:
        raise HTTPException(400, "no files uploaded")

    existing = repo.list_project_files(project_id)
    if len(existing) + len(files) > project_service.MAX_PROJECT_FILES:
        raise HTTPException(
            400,
            f"this project already has {len(existing)} knowledge files "
            f"(limit {project_service.MAX_PROJECT_FILES}).",
        )

    stored: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    for f in files:
        name = (f.filename or "knowledge.txt").replace("\\", "/").split("/")[-1]
        data = await f.read()
        if not project_service.is_supported_knowledge_file(name):
            rejected.append({"name": name, "reason": "unsupported type (text files only)"})
            continue
        if len(data) > project_service.MAX_PROJECT_FILE_BYTES:
            rejected.append(
                {
                    "name": name,
                    "reason": f"larger than {project_service.MAX_PROJECT_FILE_BYTES // 1024} KB",
                }
            )
            continue
        text = project_service.decode_knowledge_file(data)
        if text is None:
            rejected.append({"name": name, "reason": "does not look like text"})
            continue
        if not text.strip():
            rejected.append({"name": name, "reason": "empty file"})
            continue
        stored.append(repo.add_project_file(project_id, name, text.strip()))

    log.info(
        "project files uploaded",
        project_id=project_id,
        stored=len(stored),
        rejected=len(rejected),
    )
    return {"files": stored, "rejected": rejected}


@router.delete("/{project_id}/files/{file_id}", status_code=204)
def delete_file(project_id: str, file_id: str) -> None:
    if not repo.delete_project_file(project_id, file_id):
        raise HTTPException(404, "file not found")
