"""Google BigQuery connector (optional `bigquery` extra)."""

from __future__ import annotations

import time
from typing import Any

from app.connectors.base import Connector, ConnectorError, QueryResult
from app.schemas import ColumnInfo, ConnectionSpec, SourceType

_SCOPES = ("https://www.googleapis.com/auth/bigquery.readonly",)


class BigQueryConnector(Connector):
    source_type = SourceType.BIGQUERY

    def __init__(self, spec: ConnectionSpec) -> None:
        super().__init__(spec)
        self._client: Any = None

    def _bq(self) -> Any:
        if self._client is None:
            from google.cloud import bigquery  # type: ignore[import-not-found]

            cred = self.spec.credentials_json
            if cred:
                import json

                info = json.loads(cred.get_secret_value())
                self._client = bigquery.Client.from_service_account_info(info)
            else:
                self._client = bigquery.Client(project=self.spec.database)
        return self._client

    def list_schemas(self) -> list[str]:
        datasets = list(self._bq().list_datasets())
        return [d.dataset_id for d in datasets]

    def list_tables(self, schema: str | None = None) -> list[str]:
        dataset = self.resolve_schema(schema)
        if not dataset:
            return []
        return [t.table_id for t in self._bq().list_tables(dataset)]

    def describe_table(self, schema: str | None, table: str) -> list[ColumnInfo]:
        tbl = self._bq().get_table(f"{self.resolve_schema(schema)}.{table}")
        return [
            ColumnInfo(name=f.name, data_type=str(f.field_type).lower(), nullable=f.is_nullable)
            for f in tbl.schema
        ]

    def primary_keys(self, schema: str | None, table: str) -> list[str]:
        tbl = self._bq().get_table(f"{self.resolve_schema(schema)}.{table}")
        return [
            f.name for f in tbl.schema if f.field_type == "RECORD" and f.mode == "REQUIRED"
        ] or []

    def query(self, sql: str, params: dict[str, Any] | None = None) -> QueryResult:
        start = time.perf_counter()
        try:
            job = self._bq().query(sql)
            rows = [dict(r.items()) for r in list(job.result(max_results=1000))]
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(f"bigquery query failed: {exc}") from exc
        cols = list(rows[0].keys()) if rows else []
        return QueryResult(
            columns=cols,
            rows=rows,
            rowcount=len(rows),
            duration_ms=round((time.perf_counter() - start) * 1000, 2),
            truncated=len(rows) == 1000,
        )

    def quote_table(self, schema: str | None, table: str) -> str:
        return f"`{self.resolve_schema(schema)}.{table}`"
