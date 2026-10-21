"""File connectors: S3 objects and local CSV/JSON/parquet treated as tables.

Implemented as DuckDB-backed connectors so the same quality checks work on files.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from app.connectors.base import Connector, ConnectorError, QueryResult
from app.core.logging import log
from app.schemas import ColumnInfo, ConnectionSpec, SourceType

_EXTS = {
    "csv": "*.csv",
    "json": "*.json",
    "parquet": "*.parquet",
}


def _duckdb():
    import duckdb  # type: ignore[import-not-found]

    return duckdb


class _FileConnectorBase(Connector):
    """Shared DuckDB-over-files behaviour."""

    source_type = SourceType.LOCAL_CSV

    def __init__(self, spec: ConnectionSpec) -> None:
        super().__init__(spec)
        self._con: Any = None
        self._materialized: list[str] = []
        self._tmpdir: str | None = None

    def quote_table(self, schema: str | None, table: str) -> str:
        return f'"{table}"'

    # -- duckdb lifecycle --------------------------------------------------- #
    def _conn(self) -> Any:
        if self._con is None:
            self._con = _duckdb().connect()
            self._materialize()
        return self._con

    def _register_sources(self) -> list[tuple[str, str]]:
        """Return (table_name, glob_path) pairs. Implemented by subclasses."""
        raise NotImplementedError

    def _materialize(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="qualitest_")
        readers = {
            "csv": "read_csv_auto('{path}', header=true)",
            "json": "read_json_auto('{path}')",
            "parquet": "read_parquet('{path}')",
        }
        template = readers.get(self.spec.file_format, readers["csv"])
        for _table, path in self._register_sources():
            stem = Path(path).stem.replace(".", "_").replace(" ", "_") or "data"
            name = f"{stem}_{abs(hash(path)) % 100000}"
            try:
                self._con.execute(
                    f'CREATE VIEW "{name}" AS SELECT * FROM {template.format(path=path)}'
                )
            except Exception as exc:  # noqa: BLE001
                raise ConnectorError(f"cannot register file '{path}': {exc}") from exc
            self._materialized.append(name)

    def dispose(self) -> None:
        if self._con is not None:
            with contextlib.suppress(Exception):
                self._con.close()
            self._con = None
        self._materialized = []

    # -- introspection ------------------------------------------------------ #
    def list_schemas(self) -> list[str]:
        return ["files"]

    def list_tables(self, schema: str | None = None) -> list[str]:
        self._conn()
        return list(self._materialized)

    def describe_table(self, schema: str | None, table: str) -> list[ColumnInfo]:
        rows = self.query(f'DESCRIBE "{table}"').rows
        if not rows:
            return [
                ColumnInfo(name=c, data_type="unknown")
                for c in self.query(f'SELECT * FROM "{table}" LIMIT 0').columns
            ]
        return [
            ColumnInfo(
                name=r.get("column_name") or r.get("Column_Name") or r.get("column"),
                data_type=str(r.get("column_type") or r.get("Column_Type") or "unknown").lower(),
                nullable=not str(r.get("null", "YES")).upper().startswith("NO"),
            )
            for r in rows
        ]

    def primary_keys(self, schema: str | None, table: str) -> list[str]:
        return []

    # -- execution ---------------------------------------------------------- #
    def query(self, sql: str, params: dict[str, Any] | None = None) -> QueryResult:
        start = time.perf_counter()
        try:
            cur = self._conn().execute(sql, list((params or {}).values()) or None)
            cols = [d[0] for d in (cur.description or [])]
            rows = [dict(zip(cols, r, strict=False)) for r in cur.fetchmany(1000)]
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(f"duckdb query failed: {exc}") from exc
        return QueryResult(
            columns=cols,
            rows=rows,
            rowcount=len(rows),
            duration_ms=round((time.perf_counter() - start) * 1000, 2),
            truncated=len(rows) == 1000,
        )


class LocalFilesConnector(_FileConnectorBase):
    source_type = SourceType.LOCAL_CSV

    def _register_sources(self) -> list[tuple[str, str]]:
        root = Path(self.spec.path or self.spec.database or "")
        if not root.exists():
            raise ConnectorError(f"path not found: {root}")
        if root.is_file():
            return [(root.stem, str(root))]
        pattern = _EXTS.get(self.spec.file_format, "*.csv")
        return [(p.stem, str(p)) for p in sorted(root.glob(f"**/{pattern}"))]


class S3FilesConnector(_FileConnectorBase):
    source_type = SourceType.S3_CSV

    def _register_sources(self) -> list[tuple[str, str]]:
        if not (self.spec.bucket and (self.spec.key or self.spec.path)):
            raise ConnectorError("S3 source requires bucket and key/prefix")
        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise ConnectorError("boto3 not installed (pip install '.[s3]')") from exc

        creds = self.spec.credentials_json
        client_kwargs: dict[str, Any] = {"region_name": self.spec.region}
        if creds:
            import json

            client_kwargs.update(json.loads(creds.get_secret_value()))
        elif self.spec.token:
            client_kwargs["aws_session_token"] = self.spec.token.get_secret_value()
        s3 = boto3.client("s3", **client_kwargs)

        prefix = self.spec.key or self.spec.path or ""
        if not self._tmpdir:
            self._tmpdir = tempfile.mkdtemp(prefix="qualitest_")

        out: list[tuple[str, str]] = []
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.spec.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith((".csv", ".json", ".parquet")):
                    local = os.path.join(self._tmpdir, key.replace("/", "_"))
                    log.debug("downloading s3 object", key=key)
                    s3.download_file(self.spec.bucket, key, local)
                    out.append((Path(key).stem, local))
        if not out:
            raise ConnectorError(f"no files matched in s3://{self.spec.bucket}/{prefix}")
        return out
