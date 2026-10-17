"""HTTP failure modes.

The bug class these pin: an unhandled exception produced Starlette's outermost
500, which sits *outside* CORSMiddleware — so the browser received a response
without `Access-Control-Allow-Origin` and reported an ordinary server crash as
"blocked by CORS policy". Errors must reach the client *as* errors.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute, Mount
from fastapi.testclient import TestClient

from app.core.routes import SafeAPIRoute
from app.main import app


def _http_routes():
    def walk(routes):
        for r in routes:
            if isinstance(r, Mount):
                yield from walk(r.routes)
            elif isinstance(r, APIRoute):
                yield r

    yield from walk(app.routes)


def test_every_http_route_is_wrapped_by_safe_api_route() -> None:
    """`app.router.route_class = ...` does NOT reach module-level routers;
    include_router copies the routes built at import time. Each router must
    declare `route_class=SafeAPIRoute` itself. This test fails loudly if a
    new router is added without it."""
    unsafe = sorted({r.path for r in _http_routes() if not isinstance(r, SafeAPIRoute)})
    assert unsafe == []


def test_unhandled_error_response_still_carries_cors_headers() -> None:
    inner = FastAPI()
    inner.router.route_class = SafeAPIRoute

    @inner.post("/boom")
    def boom() -> dict[str, bool]:
        raise RuntimeError("ValueError: 'clickhouse' is not a valid SourceType")

    inner.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3010"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    res = TestClient(inner).post("/boom", headers={"Origin": "http://localhost:3010"})
    assert res.status_code == 500
    # the header the browser actually checks
    assert res.headers.get("access-control-allow-origin") == "http://localhost:3010"
    body = res.json()
    assert body["error"] == "internal_error"
    assert body["type"] == "RuntimeError"
    assert "clickhouse" in body["message"]


def test_http_exception_carries_cors_headers_on_the_real_app() -> None:
    """A plain 400 (invalid paths payload) on the shipped stack."""
    client = TestClient(app)
    res = client.post(
        "/api/etl/upload",
        headers={"Origin": "http://localhost:3010"},
        files={"files": ("a.sql", b"select 1")},
        data={"paths": "not-json", "mode": "folder"},
    )
    assert res.status_code == 400
    assert res.headers.get("access-control-allow-origin")
    # FastAPI's own handler renders the detail — the safe wrapper must not
    # flatten it into an anonymous internal_error
    assert "invalid paths payload" in res.json()["detail"]
