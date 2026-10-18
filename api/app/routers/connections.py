from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.connectors.factory import SUPPORTED_SOURCES, build_connector
from app.core.logging import log
from app.core.routes import SafeAPIRoute
from app.schemas import ConnectionSpec, ConnectionStatus
from app.services.ingest import _SCHEME_MAP, extract_connections_async
from app.services.requirements import split_ref
from app.store import repository as repo


def _bare(ref: str) -> str:
    return split_ref(str(ref))[1] or str(ref).lower()

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


@router.post("/test", response_model=TestConnectionResponse)
def test_connection(req: TestConnectionRequest) -> TestConnectionResponse:
    """Verify a connection, and say whether it actually holds the tables we need.

    Answering a datasource question with the wrong database is common, so the
    response reports which declared tables were found and which were not rather
    than failing silently later at plan time.
    """
    spec = req.spec
    if req.tables or req.target_tables or req.requirement_key:
        options = dict(spec.options or {})
        if req.tables:
            options["tables"] = req.tables
        if req.target_tables:
            options["target_tables"] = req.target_tables
            options.setdefault("role", "target")
        if req.requirement_key:
            options["requirement_key"] = req.requirement_key
        spec = spec.model_copy(update={"options": options})

    matched: list[str] = []
    unmatched: list[str] = []
    conn = build_connector(spec)
    try:
        status = conn.test()
        if status.ok and (req.tables or []):
            present = {
                _bare(t.get("name", ""))
                for t in conn.introspect(max_tables=200)
                .get("tables", [])
                if isinstance(t, dict) and t.get("name")
            }
            for ref in req.tables:
                (matched if _bare(ref) in present else unmatched).append(ref)
    finally:
        conn.dispose()

    if unmatched and not matched:
        status.ok = False
        status.message = (
            f"Connected, but none of the expected tables are here "
            f"({', '.join(unmatched[:4])}). Check the database and schema."
        )
    elif unmatched:
        status.message = (
            f"{status.message or ''} Found {len(matched)}/{len(req.tables)} expected "
            f"table(s); missing: {', '.join(unmatched[:4])}."
        ).strip()

    connection_id = None
    if status.ok and req.persist:
        rec = repo.save_connection(spec, req.session_id)
        connection_id = rec.id

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
async def connect_from_url(req: FromUrlRequest) -> TestConnectionResponse:
    specs = await extract_connections_async(req.url)
    if not specs:
        raise HTTPException(422, _url_diagnosis(req.url))
    return test_connection(
        TestConnectionRequest(
            spec=specs[0],
            session_id=req.session_id,
            persist=True,
            requirement_key=req.requirement_key,
            tables=req.tables or list(specs[0].options.get("tables", [])),
            target_tables=req.target_tables,
        )
    )


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