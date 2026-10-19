from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.connectors.factory import SUPPORTED_SOURCES, build_connector
from app.core.logging import log
from app.core.routes import SafeAPIRoute
from app.schemas import ConnectionSpec, ConnectionStatus, RepairReport
from app.services.connection_repair import (
    RepairResult,
    list_server_databases,
    probe,
    probe_attempt,
    repair_connection,
)
from app.services.ingest import _SCHEME_MAP, extract_connections_async
from app.store import repository as repo

router = APIRouter(
    prefix="/api/connections", tags=["connections"], route_class=SafeAPIRoute
)


class TestConnectionRequest(BaseModel):
    spec: ConnectionSpec
    session_id: str | None = None
    persist: bool = True
    # set when this connection answers a specific datasource question, so the
    # agent can bind tables deterministically instead of guessing by name
    requirement_key: str | None = None
    tables: list[str] = Field(default_factory=list)
    target_tables: list[str] = Field(default_factory=list)


class TestConnectionResponse(BaseModel):
    connection_id: str | None = None
    status: ConnectionStatus
    spec: dict[str, Any]
    matched_tables: list[str] = Field(default_factory=list)
    unmatched_tables: list[str] = Field(default_factory=list)
    # populated only when the connection as given failed and the agent searched
    # for a working one instead; `repair.suggested` is the client's to confirm
    repair: RepairReport | None = None


@router.get("/sources")
def list_sources() -> dict[str, Any]:
    """Supported data sources + the form fields the UI should render."""
    return {
        "sources": SUPPORTED_SOURCES,
        "fields_by_type": {
            "postgres": ["host", "port", "database", "schema_", "username", "password"],
            "mysql": ["host", "port", "database", "username", "password"],
            "clickhouse": ["host", "port", "database", "username", "password"],
            "snowflake": ["account", "warehouse", "role", "database", "schema_", "username", "password"],
            "bigquery": ["database", "credentials_json"],
            "databricks": ["host", "http_path", "database", "token"],
            "mssql": ["host", "port", "database", "username", "password"],
            "sqlite": ["path"],
            "local_csv": ["path", "file_format"],
            "s3_csv": ["bucket", "key", "region", "file_format", "credentials_json"],
        },
        "defaults": {
            "postgres": {"port": 5432},
            "mysql": {"port": 3306},
            "mssql": {"port": 1433},
            # the HTTP interface, not the native TCP 9000 which we cannot speak
            "clickhouse": {"port": 8123},
        },
    }


def _with_options(
    spec: ConnectionSpec,
    tables: list[str] | None = None,
    target_tables: list[str] | None = None,
    requirement_key: str | None = None,
) -> ConnectionSpec:
    """Carry the requirement's table bindings on the spec.

    Repair candidates are derived from this spec, so the bindings must ride
    along — otherwise a repaired connection could not say whether it actually
    holds the tables the ETL needs.
    """
    if not (tables or target_tables or requirement_key):
        return spec
    options = dict(spec.options or {})
    if tables:
        options["tables"] = tables
    if target_tables:
        options["target_tables"] = target_tables
        options.setdefault("role", "target")
    if requirement_key:
        options["requirement_key"] = requirement_key
    return spec.model_copy(update={"options": options})


def _requirement_hints(session_id: str | None, requirement_key: str | None) -> dict[str, Any]:
    """Addressing evidence from the ETL's own requirement (host/port/database).

    A stale hint must never *override* the user's input, so this is only ever
    fed to the repair ladder as a candidate to try, never applied directly.
    """
    if not (session_id and requirement_key):
        return {}
    try:
        for req in repo.load_session_payload(session_id).get("requirements", []):
            if req.get("key") != requirement_key:
                continue
            hints = {
                k: req.get(src)
                for k, src in (
                    ("host", "host_hint"),
                    ("port", "port_hint"),
                    ("database", "database"),
                )
                if req.get(src)
            }
            return hints
    except Exception:  # noqa: BLE001 - hints are a bonus, not a requirement
        log.debug("could not read requirement hints", exc_info=True)
    return {}


@router.post("/test", response_model=TestConnectionResponse)
def test_connection(req: TestConnectionRequest) -> TestConnectionResponse:
    """Verify a connection, and say whether it actually holds the tables we need."""
    spec = _with_options(req.spec, req.tables, req.target_tables, req.requirement_key)
    status, matched, unmatched = probe(spec, req.tables)
    connection_id = repo.save_connection(spec, req.session_id).id if (status.ok and req.persist) else None
    return TestConnectionResponse(
        connection_id=connection_id,
        status=status,
        spec=spec.redacted(),
        matched_tables=matched,
        unmatched_tables=unmatched,
    )


class FromUrlRequest(BaseModel):
    """Answer a datasource question without routing credentials through chat."""

    url: str = Field(min_length=6, max_length=600)
    session_id: str | None = None
    requirement_key: str | None = None
    tables: list[str] = Field(default_factory=list)
    target_tables: list[str] = Field(default_factory=list)


def _url_diagnosis(url: str) -> str:
    """Explain *why* a connection string was unreadable, when we can tell."""
    text = (url or "").strip()
    m = re.match(r"^([a-z][a-z0-9+.\-]*)://", text, re.I)
    if m:
        scheme = m.group(1).lower()
        if scheme not in _SCHEME_MAP:
            supported = ", ".join(sorted(set(_SCHEME_MAP)))
            return (
                f"Unsupported connection scheme {scheme!r}://. "
                f"Supported: {supported}."
            )
        return (
            f"I could not read the host/database out of that {scheme}:// URL. "
            "Expected scheme://user:password@host:port/database — a password "
            "containing '@' must be URL-encoded (%40)."
        )
    if "@" in text and ":" in text:
        return (
            "That looks like connection parts but not a URL. Try: "
            "postgresql://user:password@host:5432/database"
        )
    return (
        "That doesn't look like a connection URL. Expected something like "
        "postgresql://user:password@host:5432/database"
    )


@router.post("/from-url", response_model=TestConnectionResponse)
async def connect_from_url(req: FromUrlRequest, request: Request) -> Any:
    """Connect from a pasted URL, repairing the addressing if it fails.

    Repairs take seconds and can involve several tries, so a client that sends
    ``Accept: text/event-stream`` gets the search as it happens; everyone else
    (scripts, the flow checker) gets the same answer as before, in one JSON
    response, with the extra ``repair`` block attached.
    """
    specs = await extract_connections_async(req.url)
    if not specs:
        raise HTTPException(422, _url_diagnosis(req.url))
    given = specs[0]
    tables = req.tables or list(given.options.get("tables", []))
    spec = _with_options(given, tables, req.target_tables, req.requirement_key)
    hints = _requirement_hints(req.session_id, req.requirement_key)

    if "text/event-stream" in (request.headers.get("accept") or "").lower():
        return StreamingResponse(
            _stream_repair(spec, hints, tables, req.session_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    result = await repair_connection(
        spec,
        attempt=lambda cand: probe_attempt(cand, tables),
        hints=hints,
        list_databases=list_server_databases,
    )
    return _to_response(result, spec, tables, req.session_id, persist=True)


def _to_response(
    result: RepairResult,
    given: ConnectionSpec,
    tables: list[str],
    session_id: str | None,
    *,
    persist: bool,
) -> TestConnectionResponse:
    """Turn a repair run into the API's answer.

    The connection is persisted only when the configuration *as given* worked;
    a repaired one is offered as a suggestion for the client to confirm, so a
    guess drawn from a stale ETL hint never silently becomes the connection.
    """
    status = result.outcome.status if result.outcome and result.outcome.status else ConnectionStatus(
        ok=False, type=given.type, name=given.name, message=result.note or "connection failed"
    )
    connection_id = (
        repo.save_connection(given, session_id).id if (persist and result.ok and not result.repaired) else None
    )
    return TestConnectionResponse(
        connection_id=connection_id,
        status=status,
        spec=(result.suggested or given).redacted(),
        matched_tables=list(result.outcome.matched) if result.outcome else [],
        unmatched_tables=list(result.outcome.unmatched) if result.outcome else [],
        repair=result.report() if not result.ok or result.repaired else None,
    )


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


async def _stream_repair(
    spec: ConnectionSpec,
    hints: dict[str, Any],
    tables: list[str],
    session_id: str | None,
) -> AsyncIterator[str]:
    """Run the repair loop, forwarding each attempt to the client live.

    The search runs in its own task and hands progress to this generator over a
    queue, so a slow LLM round cannot stall the stream; a keep-alive comment is
    sent when nothing has happened for a while.
    """
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    async def run() -> None:
        try:
            result = await repair_connection(
                spec,
                attempt=lambda cand: probe_attempt(cand, tables),
                hints=hints,
                list_databases=list_server_databases,
                emit=lambda event, data: queue.put_nowait(("event", (event, data))),
            )
            response = _to_response(result, spec, tables, session_id, persist=True)
            queue.put_nowait(("done", response))
        except Exception as exc:  # noqa: BLE001 - reported to the client as an event
            log.warning("connection repair stream failed", error=str(exc))
            queue.put_nowait(("done", None))
        finally:
            queue.put_nowait(("end", None))

    task = asyncio.create_task(run())
    try:
        while True:
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=10)
            except TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if kind == "end":
                break
            if kind == "event":
                name, data = payload
                yield _sse(name, data)
            elif kind == "done" and payload is not None:
                yield _sse("result", payload.model_dump(mode="json"))
    finally:
        if not task.done():
            task.cancel()


@router.get("")
def list_connections(session_id: str | None = None) -> dict[str, Any]:
    conns = repo.list_connections(session_id)
    return {"connections": [c.redacted() for c in conns], "count": len(conns)}


@router.get("/{connection_id}")
def get_connection(connection_id: str) -> dict[str, Any]:
    spec = repo.get_connection(connection_id)
    if not spec:
        raise HTTPException(404, "connection not found")
    return spec.redacted()


@router.post("/{connection_id}/introspect")
def introspect(connection_id: str, max_tables: int = 40) -> dict[str, Any]:
    spec = repo.get_connection(connection_id)
    if not spec:
        raise HTTPException(404, "connection not found")
    conn = build_connector(spec)
    try:
        return conn.introspect(max_tables=max_tables)
    except Exception as exc:  # noqa: BLE001
        log.warning("introspect endpoint failed", error=str(exc))
        raise HTTPException(400, f"introspection failed: {exc}") from exc
    finally:
        conn.dispose()


@router.delete("/{connection_id}", status_code=204)
def delete_connection(connection_id: str) -> None:
    return None


class ValidateSpecRequest(BaseModel):
    spec: ConnectionSpec
    table: str | None = Field(default=None)


@router.post("/validate-spec")
def validate_spec(req: ValidateSpecRequest) -> dict[str, Any]:
    """Dry-run the mapping between a spec and a user-declared table binding."""
    return {
        "type": str(req.spec.type),
        "table": req.table,
        "supported": str(req.spec.type) in {s["type"] for s in SUPPORTED_SOURCES},
        "read_only": req.spec.read_only,
    }