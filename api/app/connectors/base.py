"""Abstract data source connector.

Every connector is read-only by default: the agent only introspects and runs
aggregate validation queries, it never mutates user data.
"""

from __future__ import annotations

import abc
import contextlib
import re
import time
from dataclasses import dataclass, field
from typing import Any

from app.schemas import ColumnInfo, ConnectionSpec, ConnectionStatus, SourceType


@dataclass(slots=True)
class QueryResult:
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    rowcount: int = 0
    duration_ms: float = 0.0
    truncated: bool = False


class ConnectorError(RuntimeError):
    """Raised for connection/query failures surfaced to the agent."""


class Connector(abc.ABC):
    source_type: SourceType
    default_schema: str | None = None

    def __init__(self, spec: ConnectionSpec) -> None:
        self.spec = spec

    # -- lifecycle ---------------------------------------------------------- #
    @abc.abstractmethod
    def _engine(self) -> Any: ...

    def dispose(self) -> None:
        eng = getattr(self, "_eng", None)
        if eng is not None:
            with contextlib.suppress(Exception):  # best-effort cleanup
                eng.dispose()
            self._eng = None

    # -- introspection ------------------------------------------------------ #
    @abc.abstractmethod
    def list_schemas(self) -> list[str]: ...

    @abc.abstractmethod
    def list_tables(self, schema: str | None = None) -> list[str]: ...

    @abc.abstractmethod
    def describe_table(self, schema: str | None, table: str) -> list[ColumnInfo]: ...

    @abc.abstractmethod
    def primary_keys(self, schema: str | None, table: str) -> list[str]: ...

    def row_count(self, schema: str | None, table: str) -> int | None:
        try:
            res = self.query(f"SELECT COUNT(*) AS c FROM {self.quote_table(schema, table)}")
            if res.rows:
                return int(list(res.rows[0].values())[0])
        except Exception as exc:  # noqa: BLE001 - optional signal
            raise ConnectorError(str(exc)) from exc
        return None

    # -- execution ---------------------------------------------------------- #
    @abc.abstractmethod
    def query(self, sql: str, params: dict[str, Any] | None = None) -> QueryResult: ...

    def fetch_sample(
        self, schema: str | None, table: str, limit: int = 20, columns: list[str] | None = None
    ) -> QueryResult:
        cols = ", ".join(f'"{c}"' for c in columns) if columns else "*"
        return self.query(
            f"SELECT {cols} FROM {self.quote_table(schema, table)} LIMIT {int(limit)}"
        )

    # -- helpers ------------------------------------------------------------ #
    def quote_table(self, schema: str | None, table: str) -> str:
        sch = schema or self.default_schema
        if sch:
            return f'"{sch}"."{table}"'
        return f'"{table}"'

    def resolve_schema(self, schema: str | None) -> str | None:
        return schema or self.default_schema or self.spec.schema_

    def regex_mismatch(self, col_sql: str, regex: str) -> str:
        """A boolean SQL expression: "this column fails the pattern".

        Regex syntax is the only place our otherwise-ANSI probes truly
        diverge across engines. The default is the Postgres `!~` operator
        (byte-for-byte what the compiler emitted before dialects existed);
        engines without it — ClickHouse — override with their own function.
        """
        escaped = "'" + regex.replace("'", "''") + "'"
        return f"{col_sql} !~ {escaped}"

    def test(self) -> ConnectionStatus:
        start = time.perf_counter()
        try:
            schemas = self.list_schemas()
            tables: list[str] = []
            first_schema = schemas[0] if schemas else self.resolve_schema(None)
            if first_schema:
                tables = self.list_tables(first_schema)[:50]
            return ConnectionStatus(
                ok=True,
                type=self.source_type,
                name=self.spec.name,
                latency_ms=round((time.perf_counter() - start) * 1000, 2),
                schemas=schemas,
                tables=[],
                message=f"connected; {len(schemas)} schema(s), "
                f"{len(tables)} table(s) in '{first_schema}'",
            )
        except Exception as exc:  # noqa: BLE001 - reported to user
            return ConnectionStatus(
                ok=False,
                type=self.source_type,
                name=self.spec.name,
                latency_ms=round((time.perf_counter() - start) * 1000, 2),
                message=_friendly_error(exc, spec=self.spec),
            )

    def introspect(
        self, schema: str | None = None, max_tables: int = 30, max_schemas: int = 6
    ) -> dict[str, Any]:
        """Describe tables + columns.

        With an explicit schema we look there only. Otherwise we sweep up to
        `max_schemas` schemas so an ETL that references `raw.orders` can be
        bound even when the connection's default schema is `public`.
        """
        schemas = self.list_schemas()
        # The declared schema comes first, but we still sweep the others: an ETL
        # often spans `analytics` (target) and `raw` (sources) in one database.
        if schema:
            targets = [schema, *(s for s in schemas if s != schema)][: max(max_schemas, 1)]
        else:
            targets = schemas[:max_schemas]

        tables: list[dict[str, Any]] = []
        for sch in targets:
            if len(tables) >= max_tables * len(targets):
                break
            try:
                names = self.list_tables(sch)
            except Exception as exc:  # noqa: BLE001 - unreadable schema is skipped
                tables.append({"schema": sch, "error": _friendly_error(exc)})
                continue
            for t in names:
                if len(tables) >= max_tables * len(targets):
                    break
                try:
                    cols = self.describe_table(sch, t)
                    pks = self.primary_keys(sch, t)
                    pk_set = set(pks)
                    for c in cols:
                        c.is_primary_key = c.name in pk_set
                    try:
                        rc = self.row_count(sch, t)
                    except Exception:  # noqa: BLE001 - big tables / perms
                        rc = None
                    tables.append(
                        {
                            "schema": sch,
                            "name": t,
                            "row_count": rc,
                            "primary_keys": pks,
                            "columns": [c.model_dump(by_alias=True) for c in cols],
                        }
                    )
                except Exception as exc:  # noqa: BLE001 - skip unreadable table
                    tables.append({"schema": sch, "name": t, "error": _friendly_error(exc)})

        return {"schemas": schemas, "schema": schema or self.resolve_schema(None), "tables": tables}


def _friendly_error(exc: Exception, spec: ConnectionSpec | None = None) -> str:
    msg = str(exc).strip().splitlines()[0] if str(exc) else exc.__class__.__name__
    msg = msg[:400]
    if spec is not None and (hint := localhost_hint(spec, msg)):
        msg = f"{msg} — {hint}"
    return msg


# Whoever runs the agent runs it in a container. "localhost" inside a
# container is the container itself: a database on the user's machine (or on
# another container's published port) is *never* under that name. A refusal
# against localhost therefore earns an explicit environmental explanation —
# the single most common connection failure, and the most baffling otherwise.
_LOCALHOST_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
_UNREACHABLE_RE = re.compile(
    r"refused|unreachable|no route to host|cannot assign requested address|timed?\s?-?out",
    re.I,
)


def localhost_hint(spec: ConnectionSpec, error_text: str) -> str | None:
    """Explain *why* localhost could fail given the containerized agent."""
    host = (spec.host or "").lower().strip()
    if not host or host.rstrip(".") not in _LOCALHOST_HOSTS:
        return None
    if not _UNREACHABLE_RE.search(error_text):
        return None
    return (
        "'localhost' here is the agent's own container — a database on your "
        "machine never answers that name from inside it. If the database runs "
        "on your host (Docker Desktop) use 'host.docker.internal' instead; "
        "another container's service name works too."
    )


def scalar(res: QueryResult, default: float | None = None) -> float | None:
    if not res.rows:
        return default
    try:
        return float(list(res.rows[0].values())[0])
    except (TypeError, ValueError):
        return default