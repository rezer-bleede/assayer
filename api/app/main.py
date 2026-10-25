from __future__ import annotations

import contextlib
import hashlib
import time
from collections.abc import AsyncIterator
from typing import Any

import orjson
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from app.core.config import settings
from app.core.identity import guest_id_context, user_id_context, workspace_id_context
from app.core.llm import llm_available
from app.core.logging import configure_logging, log
from app.core.routes import SafeAPIRoute
from app.routers import agent, auth, chat, connections, etl, projects, runs, threads
from app.store import db as db_store
from app.store.db import (
    GuestSessionRow,
    LoginSessionRow,
    WorkspaceMemberRow,
    WorkspaceRow,
    utcnow_str,
)

configure_logging()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    db_store.init_db()
    app.state.auth_ready = True
    log.info(
        "api ready",
        env=settings.environment,
        llm="configured" if llm_available() else "not-configured",
    )
    yield


app = FastAPI(
    title="Assayer API",
    version="0.1.0",
    description=(
        "Agentic testing and QA for data, ETL, web applications, and UI/UX. "
        "Share a project or connect a target to build a focused test plan and review results."
    ),
    default_response_class=ORJSONResponse,
    lifespan=lifespan,
)
app.state.auth_ready = False

app.router.route_class = SafeAPIRoute

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Assayer-Messages-Remaining", "X-Assayer-Login-Required"],
)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, same_site="lax", https_only=settings.environment == "production")

app.include_router(connections.router)
app.include_router(etl.router)
app.include_router(runs.router)
app.include_router(chat.router)
app.include_router(agent.router)
app.include_router(threads.router)
app.include_router(projects.router)
app.include_router(auth.router)


@app.middleware("http")
async def require_account(request: Request, call_next):
    path = request.url.path
    if (not app.state.auth_ready) or path.startswith(("/api/auth/", "/healthz", "/readyz", "/docs", "/openapi.json", "/redoc")):
        return await call_next(request)
    with db_store.SessionLocal() as db:
        token = request.cookies.get("assayer_session")
        session = db.scalar(
            select(LoginSessionRow).where(
                LoginSessionRow.token_hash == hashlib.sha256(token.encode()).hexdigest()
            )
        ) if token else None
        guest = None
        if session is None or session.expires_at <= utcnow_str():
            session = None
            guest_token = request.cookies.get("assayer_guest")
            guest = db.scalar(select(GuestSessionRow).where(
                GuestSessionRow.token_hash == hashlib.sha256(guest_token.encode()).hexdigest()
            )) if guest_token else None
            if guest is None or guest.expires_at <= utcnow_str():
                return ORJSONResponse({"detail": "guest session required"}, status_code=401)
        request.state.guest_id = guest.id if guest else None
        if guest and (path.startswith("/api/projects") or path == "/api/threads/move"):
            return ORJSONResponse({"detail": "sign in to use projects"}, status_code=401)
        user_id = session.user_id if session else guest.id
        workspace_id = None
        if user_id:
            requested_workspace = request.cookies.get("assayer_workspace")
            membership = db.scalar(select(WorkspaceMemberRow).where(
                WorkspaceMemberRow.user_id == user_id,
                WorkspaceMemberRow.workspace_id == requested_workspace,
            )) if requested_workspace else None
            if membership:
                workspace_id = membership.workspace_id
            else:
                personal = db.scalar(select(WorkspaceRow).join(
                    WorkspaceMemberRow, WorkspaceMemberRow.workspace_id == WorkspaceRow.id
                ).where(WorkspaceMemberRow.user_id == user_id, WorkspaceRow.kind == "personal"))
                workspace_id = personal.id if personal else None
    marker = user_id_context.set(user_id)
    workspace_marker = workspace_id_context.set(workspace_id)
    guest_marker = guest_id_context.set(guest.id if guest else None)
    try:
        return await call_next(request)
    finally:
        user_id_context.reset(marker)
        workspace_id_context.reset(workspace_marker)
        guest_id_context.reset(guest_marker)


@app.middleware("http")
async def timing(request: Request, call_next):
    t0 = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Response-Time-ms"] = f"{(time.perf_counter() - t0) * 1000:.1f}"
    return response


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"status": "ok", "app": settings.app_name, "llm": llm_available()}


@app.get("/readyz")
def readyz() -> dict[str, Any]:
    from sqlalchemy import text

    from app.store.db import engine

    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        db_ok = True
    except Exception as exc:  # noqa: BLE001
        log.warning("readiness db check failed", error=str(exc))
        db_ok = False
    return {"status": "ok" if db_ok else "degraded", "db": db_ok, "llm": llm_available()}


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "name": settings.app_name,
        "docs": "/docs",
        "websocket": "/ws/chat",
        "endpoints": [
            "POST /api/connections/test",
            "POST /api/etl/analyze",
            "POST /api/runs/plan",
            "POST /api/runs/{id}/approve",
            "GET  /api/runs/{id}",
            "WS   /ws/chat",
        ],
    }


__all__ = ["app", "orjson"]
