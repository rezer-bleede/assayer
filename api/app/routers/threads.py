"""Thread persistence for OpenUI's restStorage adapter."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.routes import SafeAPIRoute
from app.schemas import ChatMessage, ChatRole, new_id
from app.store import repository as repo
from app.store.db import utcnow_str

router = APIRouter(prefix="/api/threads", tags=["threads"], route_class=SafeAPIRoute)


class CreateThreadRequest(BaseModel):
    messages: list[dict[str, Any]] = Field(default_factory=list)
    projectId: str | None = None


class UpdateThreadRequest(BaseModel):
    id: str | None = None
    title: str | None = None
    createdAt: str | int | None = None


class MoveThreadRequest(BaseModel):
    threadId: str
    projectId: str | None = None


def _message(raw: dict[str, Any]) -> ChatMessage:
    role = str(raw.get("role") or "user")
    if role not in {r.value for r in ChatRole}:
        role = str(ChatRole.USER)
    content = raw.get("content")
    if not isinstance(content, str):
        content = "" if content is None else str(content)
    return ChatMessage(
        id=str(raw.get("id") or new_id()),
        role=ChatRole(role),
        content=content,
    )


def _title(messages: list[dict[str, Any]]) -> str:
    for raw in messages:
        if str(raw.get("role")) != str(ChatRole.USER):
            continue
        text = " ".join(str(raw.get("content") or "").split())
        if text:
            return text[:60]
    return "New audit"


@router.get("/get")
def list_threads(cursor: str | None = Query(default=None)) -> dict[str, Any]:
    threads = repo.list_threads()
    return {"threads": threads}


@router.post("/create")
def create_thread(req: CreateThreadRequest) -> dict[str, Any]:
    thread_id = new_id()
    repo.ensure_session(thread_id)
    title = _title(req.messages)
    payload: dict[str, Any] = {"title": title, "stage": "need_etl"}
    if req.projectId and repo.get_project(req.projectId) is not None:
        payload["project_id"] = req.projectId
    repo.update_session_payload(thread_id, payload)
    for raw in req.messages:
        repo.append_message(thread_id, _message(raw))
    return {
        "id": thread_id,
        "title": title,
        "createdAt": utcnow_str(),
        "projectId": payload.get("project_id"),
    }


@router.post("/move")
def move_thread(req: MoveThreadRequest) -> dict[str, Any]:
    """File a chat into a project, or unfile it when projectId is null."""
    if req.projectId is not None and repo.get_project(req.projectId) is None:
        raise HTTPException(404, "project not found")
    moved = repo.set_thread_project(req.threadId, req.projectId)
    if moved is None:
        raise HTTPException(404, "thread not found")
    return moved


@router.get("/get/{thread_id}")
def get_thread_messages(thread_id: str) -> list[dict[str, Any]]:
    return [m.model_dump(mode="json") for m in repo.list_messages(thread_id)]


@router.patch("/update/{thread_id}")
def update_thread(thread_id: str, req: UpdateThreadRequest) -> dict[str, Any]:
    title = (req.title or "").strip() or "New audit"
    updated = repo.rename_thread(thread_id, title)
    if updated is None:
        raise HTTPException(404, "thread not found")
    return updated


@router.delete("/delete/{thread_id}", status_code=204)
def delete_thread(thread_id: str) -> None:
    repo.delete_thread(thread_id)
    return None
