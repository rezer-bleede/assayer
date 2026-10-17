from __future__ import annotations

import contextlib
import time
from collections.abc import AsyncIterator
from typing import Any

import orjson
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse
from fastapi.routing import APIRoute

from app.core.config import settings
from app.core.llm import llm_available
from app.core.logging import configure_logging, log
from app.routers import chat, connections, etl, runs
from app.store.db import init_db

configure_logging()


class SafeAPIRoute(APIRoute):
    """Never leak stack traces or credentials in error bodies."""

    def get_route_handler(self):  # type: ignore[no-untyped-def]
        original = super().get_route_handler()

        async def handler(request: Request) -> Any:
            try:
                return await original(request)
            except Exception as exc:  # noqa: BLE001
                log.exception("request failed", path=request.url.path)
                return ORJSONResponse(
                    status_code=500,
                    content={
                        "error": "internal_error",
                        "type": type(exc).__name__,
                        "message": str(exc)[:400],
                        "path": request.url.path,
                    },
                )

        return handler


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_db()
    log.info(
        "api ready",
        env=settings.environment,
        llm="configured" if llm_available() else "not-configured",
    )
    yield


app = FastAPI(
    title="data-qualitest-agent API",
    version="0.1.0",
    description=(
        "Agentic data testing and data quality auditing. "
        "Provide connections + ETL, the agent derives a test plan and runs it."
    ),
    default_response_class=ORJSONResponse,
    lifespan=lifespan,
)

app.router.route_class = SafeAPIRoute

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(connections.router)
app.include_router(etl.router)
app.include_router(runs.router)
app.include_router(chat.router)


@app.middleware("http")
async def timing(request: Request, call_next):  # type: ignore[no-untyped-def]
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