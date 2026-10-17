"""Databricks connector via the official SQL connector (optional extra)."""

from __future__ import annotations

import time
from typing import Any

from app.connectors.base import Connector, ConnectorError, QueryResult
from app.schemas import ColumnInfo, ConnectionSpec, SourceType


class DatabricksConnector(Connector):
    source_type = SourceType.DATABRICKS

    def __init__(self, spec: ConnectionSpec) -> None:
        super().__init__(spec)
        self._conn: Any = None

    def _connection(self) -> Any:
        if self._conn is None:
            from databricks import sql  # type: ignore[import-not-found]

            self._conn = sql.connect(
                server_hostname=self.spec.host,
                http_path=self.spec.http_path,
                access_token=self.spec.token.get_secret_value() if self.spec.token else None,
                catalog=self.spec.database,
            )
        return self._conn

    def _run(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params or {})
            if cur.description is None:
                return []
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]

    def list_schemas(self) -> list[str]:
        return [
            r["schemaName"]
            for r in self._run("SHOW SCHEMAS IN CATALOG")
            if r.get("schemaName", "") not in {"information_schema"}
        ]

    def list_tables(self, schema: str | None = None) -> list[str]:
        sql = f"SHOW TABLES IN CATALOG {self.spec.database}"
        return [r["tableName"] for r in self._run(sql)]

    def describe_table(self, schema: str | None, table: str) -> list[ColumnInfo]:
        rows = self._run(f"DESCRIBE TABLE {self.quote_table(schema, table)}")
        return [
            ColumnInfo(
                name=r["col_name"],
                data_type=str(r["data_type"]).lower(),
                nullable=str(r.get("nullable", "true")).lower() == "true",
            )
            for r in rows
            if r.get("col_name")
        ]

    def primary_keys(self, schema: str | None, table: str) -> list[str]:
        try:
            rows = self._run(f"SHOW PRIMARY KEYS IN TABLE {self.quote_table(schema, table)}")
            return [r["column_name"] for r in rows]
        except ConnectorError:
            return []

    def query(self, sql: str, params: dict[str, Any] | None = None) -> QueryResult:
        start = time.perf_counter()
        try:
            rows = self._run(sql, params)
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(f"databricks query failed: {exc}") from exc
        cols = list(rows[0].keys()) if rows else []
        return QueryResult(
            columns=cols,
            rows=rows[:1000],
            rowcount=min(len(rows), 1000),
            duration_ms=round((time.perf_counter() - start) * 1000, 2),
            truncated=len(rows) > 1000,
        )

    def quote_table(self, schema: str | None, table: str) -> str:
        parts = [p for p in (self.spec.database, self.resolve_schema(schema), table) if p]
        return ".".join(f"`{p}`" for p in parts)