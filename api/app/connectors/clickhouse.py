"""ClickHouse connector via the official HTTP driver (clickhouse-connect).

ClickHouse is not Postgres: it has no information_schema, no `!~`, and its
"database" is what everyone else calls a schema. Everything engine-specific
therefore lives here (introspection against `system.*`, the regex dialect in
`regex_mismatch`) and nothing in the planner or compiler.

The session is opened with `readonly=2`, so even if a probe somehow escaped
`assert_readonly`, the server itself refuses anything but SELECTs.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

from app.connectors.base import Connector, ConnectorError, QueryResult, localhost_hint
from app.schemas import ColumnInfo, ConnectionSpec, SourceType

# ClickHouse exposes these as databases, not as user data worth sweeping
_HIDDEN_DATABASES = ("INFORMATION_SCHEMA", "information_schema", "system")


def _q_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _q_str(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


# ClickHouse's native TCP protocol (9000 family) is a different wire protocol
# from the HTTP interface the clickhouse-connect driver speaks. Connecting
# here fails even when the server is perfectly healthy, so failure on these
# ports earns an explicit explanation, not just a raw socket error.
_NATIVE_TCP_PORTS = {9000, 9009, 9440}


class ClickHouseConnector(Connector):
    source_type = SourceType.CLICKHOUSE

    def __init__(self, spec: ConnectionSpec) -> None:
        super().__init__(spec)
        self._client: Any = None

    # -- lifecycle ---------------------------------------------------------- #
    def _engine(self) -> Any:
        return self._client

    def _connection(self) -> Any:
        if self._client is None:
            import clickhouse_connect

            options = self.spec.options or {}
            port = self.spec.port
            secure = str(options.get("secure", "")).lower() in {"1", "true", "yes"} or port == 8443
            try:
                self._client = clickhouse_connect.get_client(
                    host=self.spec.host,
                    port=port,
                    username=self.spec.username or "default",
                    password=self.spec.password.get_secret_value()
                    if self.spec.password
                    else "",
                    database=self.spec.database,
                    secure=secure,
                    settings={"readonly": int(options.get("readonly", 2))},
                )
            except Exception as exc:  # noqa: BLE001 - surfaced as ConnectorError
                msg = f"cannot connect to ClickHouse: {exc}"
                try:
                    port = int(port) if port is not None else None
                except (TypeError, ValueError):
                    port = None
                if port in _NATIVE_TCP_PORTS or (
                    port is not None and 9000 <= port <= 9099
                ):
                    msg += (
                        f" — port {port} looks like the ClickHouse native TCP interface, "
                        f"which does not speak HTTP; use the HTTP port (default 8123, "
                        f"TLS 8443)"
                    )
                if hint := localhost_hint(self.spec, msg):
                    msg = f"{msg}.\n{hint}"
                raise ConnectorError(msg) from exc
        return self._client

    def dispose(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):  # best-effort cleanup
                self._client.close()
            self._client = None

    # -- introspection (system.*; ClickHouse has no information_schema) ----- #
    @staticmethod
    def _column_names(res: Any) -> list[str]:
        """Column names across driver versions.

        clickhouse-connect renamed things under us: older releases put the
        column *names* in `result_columns`, while 1.11 puts the column-major
        *values* there and moves the names to `column_names`. Trust
        `column_names` when the driver provides it; otherwise
        `result_columns` already holds the names.
        """
        names = getattr(res, "column_names", None)
        return list(names) if names else list(res.result_columns)

    def _rows(self, sql: str) -> list[dict[str, Any]]:
        try:
            res = self._connection().query(sql)
        except ConnectorError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(f"clickhouse query failed: {exc}") from exc
        cols = self._column_names(res)
        return [dict(zip(cols, row, strict=False)) for row in res.result_rows]

    def list_schemas(self) -> list[str]:
        sql = (
            "SELECT name FROM system.databases "
            f"WHERE name NOT IN ({', '.join(_q_str(n) for n in _HIDDEN_DATABASES)}) "
            "ORDER BY name"
        )
        return [str(r["name"]) for r in self._rows(sql)]

    def list_tables(self, schema: str | None = None) -> list[str]:
        db = schema or self.spec.database or "default"
        sql = (
            "SELECT name FROM system.tables WHERE database = "
            f"{_q_str(db)} AND engine NOT LIKE '%View' ORDER BY name"
        )
        return [str(r["name"]) for r in self._rows(sql)]

    def describe_table(self, schema: str | None, table: str) -> list[ColumnInfo]:
        db = schema or self.spec.database or "default"
        sql = (
            "SELECT name, type, is_nullable FROM system.columns "
            f"WHERE database = {_q_str(db)} AND table = {_q_str(table)} ORDER BY position"
        )
        return [
            ColumnInfo(
                name=str(r["name"]),
                # Nullable(String) is a wrapper, not a type — report the type
                data_type=str(r["type"]).removeprefix("Nullable(").removesuffix(")"),
                nullable=bool(int(r["is_nullable"]))
                or str(r["type"]).startswith("Nullable"),
            )
            for r in self._rows(sql)
        ]

    def primary_keys(self, schema: str | None, table: str) -> list[str]:
        db = schema or self.spec.database or "default"
        sql = (
            "SELECT name FROM system.columns "
            f"WHERE database = {_q_str(db)} AND table = {_q_str(table)} "
            "AND is_in_primary_key = 1 ORDER BY position"
        )
        try:
            return [str(r["name"]) for r in self._rows(sql)]
        except ConnectorError:
            return []

    # -- execution ----------------------------------------------------------- #
    def query(self, sql: str, params: dict[str, Any] | None = None) -> QueryResult:
        if params:
            # ClickHouse binds server-side with {name:Type} placeholders, a
            # different syntax from every other connector's :name params.
            # Silently ignoring them would run a wrong query; say nothing,
            # or run the wrong thing, are both off the table.
            raise ConnectorError(
                "clickhouse checks must inline their parameters "
                "(ClickHouse uses server-side {name:Type} placeholders)"
            )
        start = time.perf_counter()
        try:
            res = self._connection().query(sql)
        except ConnectorError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(f"clickhouse query failed: {exc}") from exc
        cols = self._column_names(res)
        rows = [dict(zip(cols, row, strict=False)) for row in res.result_rows[:1000]]
        return QueryResult(
            columns=cols,
            rows=rows,
            rowcount=min(len(res.result_rows), 1000),
            duration_ms=round((time.perf_counter() - start) * 1000, 2),
            truncated=len(res.result_rows) > 1000,
        )

    # -- dialect ------------------------------------------------------------ #
    def quote_table(self, schema: str | None, table: str) -> str:
        db = schema or self.spec.database
        if db:
            return f"{_q_ident(db)}.{_q_ident(table)}"
        return _q_ident(table)

    def regex_mismatch(self, col_sql: str, regex: str) -> str:
        """ClickHouse has no `!~`; `match()` tests, so failure is the negation.

        Backslashes are doubled because ClickHouse processes string escapes:
        the shipped email pattern contains `\\s`, which would otherwise arrive
        at the regex engine as a bare `s` — a *silently wrong* check, the one
        failure mode an audit tool must never have.
        """
        escaped = _q_str(regex)
        return f"NOT match({col_sql}, {escaped})"
