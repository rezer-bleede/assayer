"""SQLAlchemy-backed connector for PostgreSQL / MySQL / MSSQL / SQLite."""

from __future__ import annotations

from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from app.connectors.base import Connector, ConnectorError, QueryResult
from app.core.logging import log
from app.schemas import ColumnInfo, ConnectionSpec, SourceType

_SQLALCHEMY_DIALECTS: dict[SourceType, str] = {
    SourceType.POSTGRES: "postgresql+psycopg",
    SourceType.MYSQL: "mysql+pymysql",
    SourceType.MSSQL: "mssql+pyodbc",
    SourceType.SQLITE: "sqlite",
}


class SqlAlchemyConnector(Connector):
    """Generic SQL connector. Subclasses only declare dialect + metadata queries."""

    source_type: SourceType = SourceType.POSTGRES

    @property
    def default_schema(self) -> str | None:
        return None

    # metadata query templates, overridden per dialect
    schema_query: str = "SELECT schema_name FROM information_schema.schemata"
    table_query: str = (
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = :schema AND table_type IN ('BASE TABLE','VIEW')"
    )
    columns_query: str = (
        "SELECT column_name, data_type, is_nullable "
        "FROM information_schema.columns WHERE table_schema = :schema AND table_name = :table "
        "ORDER BY ordinal_position"
    )
    pks_query: str = (
        "SELECT kcu.column_name FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu "
        "ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema "
        "WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = :schema "
        "AND tc.table_name = :table"
    )

    def __init__(self, spec: ConnectionSpec) -> None:
        super().__init__(spec)
        self._eng: Any = None

    # -- connection --------------------------------------------------------- #
    def _url(self) -> str:
        dialect = _SQLALCHEMY_DIALECTS[self.source_type]
        s = self.spec
        if self.source_type == SourceType.SQLITE:
            return f"sqlite:///{s.path or s.database or ':memory:'}"

        creds = ""
        if s.username:
            from urllib.parse import quote_plus

            pwd = quote_plus(s.password.get_secret_value()) if s.password else ""
            creds = f"{quote_plus(s.username)}:{pwd}@"

        query = []
        if self.source_type == SourceType.MSSQL:
            query.append("driver=ODBC+Driver+18+SQL+Server")
            query.append("TrustServerCertificate=yes")
        url = (
            f"{dialect}://{creds}{s.host or 'localhost'}"
            f":{s.port or self.default_port}/{s.database or ''}"
        )
        if query:
            url += "?" + "&".join(query)
        return url

    @property
    def default_port(self) -> int:
        return {
            SourceType.POSTGRES: 5432,
            SourceType.MYSQL: 3306,
            SourceType.MSSQL: 1433,
        }.get(self.source_type, 5432)

    def _engine(self) -> Any:
        if self._eng is None:
            url = self._url()
            log.info(
                "connecting",
                type=str(self.source_type),
                host=self.spec.host,
                database=self.spec.database,
            )
            self._eng = create_engine(
                url,
                pool_pre_ping=True,
                pool_size=5,
                max_overflow=10,
                pool_timeout=15,
                future=True,
            )
        return self._eng

    def _read_only_sql(self) -> str | None:
        """Session statement that pins the connection to read-only, if supported."""
        if not self.spec.read_only:
            return None
        if self.source_type == SourceType.POSTGRES:
            return "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"
        if self.source_type == SourceType.MYSQL:
            return "SET SESSION TRANSACTION READ ONLY"
        if self.source_type == SourceType.MSSQL:
            return None  # enforced per-transaction; not worth an extra round-trip
        return None

    def _conn(self) -> Any:
        conn = self._engine().connect()
        if ro := self._read_only_sql():
            try:
                conn.exec_driver_sql(ro)
            except Exception as exc:  # noqa: BLE001 - non-fatal, we stay read-only by convention
                log.warning("could not pin connection read-only", sql=ro, error=str(exc))
        return conn

    # -- introspection ------------------------------------------------------ #
    def list_schemas(self) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(text(self.schema_query)).mappings().all()
        skip = {"pg_catalog", "information_schema", "pg_toast"}
        names = [list(r.values())[0] for r in rows]
        return [n for n in names if n not in skip]

    def list_tables(self, schema: str | None = None) -> list[str]:
        sch = self.resolve_schema(schema) or "public"
        q = self.table_query
        if self.source_type == SourceType.SQLITE:
            rows = self.query(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
                "AND name NOT LIKE 'sqlite_%'"
            )
            return [list(r.values())[0] for r in rows.rows]
        with self._conn() as conn:
            rows = conn.execute(text(q), {"schema": sch}).mappings().all()
        return sorted({list(r.values())[0] for r in rows})

    def describe_table(self, schema: str | None, table: str) -> list[ColumnInfo]:
        if self.source_type == SourceType.SQLITE:
            res = self.query(f'PRAGMA table_info("{table}")')
            return [
                ColumnInfo(
                    name=r.get("name"),
                    data_type=str(r.get("type") or "TEXT"),
                    nullable=not bool(r.get("notnull")),
                    is_primary_key=bool(r.get("pk")),
                )
                for r in res.rows
            ]

        sch = self.resolve_schema(schema) or "public"
        with self._conn() as conn:
            rows = (
                conn.execute(text(self.columns_query), {"schema": sch, "table": table})
                .mappings()
                .all()
            )
        return [
            ColumnInfo(
                name=list(r.values())[0],
                data_type=str(list(r.values())[1]).lower(),
                nullable=str(list(r.values())[2]).upper() in {"YES", "1", "TRUE"},
            )
            for r in rows
        ]

    def primary_keys(self, schema: str | None, table: str) -> list[str]:
        if self.source_type == SourceType.SQLITE:
            cols = self.describe_table(schema, table)
            return [c.name for c in cols if c.is_primary_key]
        sch = self.resolve_schema(schema) or "public"
        with self._conn() as conn:
            rows = (
                conn.execute(text(self.pks_query), {"schema": sch, "table": table}).mappings().all()
            )
        return [list(r.values())[0] for r in rows]

    # -- execution ---------------------------------------------------------- #
    def query(self, sql: str, params: dict[str, Any] | None = None) -> QueryResult:
        import time

        start = time.perf_counter()
        try:
            with self._conn() as conn:
                cursor = conn.execute(text(sql), params or {})
                if cursor.returns_rows:
                    cols = list(cursor.keys())
                    limit = 1000
                    rows = [dict(r) for r in cursor.mappings().fetchmany(limit)]
                    truncated = len(rows) == limit
                else:
                    cols, rows, truncated = [], [], False
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(f"{self.source_type} query failed: {exc}") from exc
        return QueryResult(
            columns=cols,
            rows=rows,
            rowcount=len(rows),
            duration_ms=round((time.perf_counter() - start) * 1000, 2),
            truncated=truncated,
        )


class PostgresConnector(SqlAlchemyConnector):
    source_type = SourceType.POSTGRES

    @property
    def default_schema(self) -> str | None:
        return "public"

    @property
    def default_port(self) -> int:
        return 5432


class MySQLConnector(SqlAlchemyConnector):
    source_type = SourceType.MYSQL

    schema_query = (
        "SELECT schema_name FROM information_schema.schemata "
        "WHERE schema_name NOT IN ('information_schema','mysql','performance_schema','sys')"
    )

    @property
    def default_port(self) -> int:
        return 3306


class MSSQLConnector(SqlAlchemyConnector):
    source_type = SourceType.MSSQL

    @property
    def default_schema(self) -> str | None:
        return "dbo"

    schema_query = "SELECT name FROM sys.schemas"
    table_query = "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = :schema"
    columns_query = (
        "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = :schema AND TABLE_NAME = :table ORDER BY ORDINAL_POSITION"
    )

    @property
    def default_port(self) -> int:
        return 1433


class SQLiteConnector(SqlAlchemyConnector):
    source_type = SourceType.SQLITE

    def _engine(self) -> Any:
        if self._eng is None:
            self._eng = create_engine(
                self._url(),
                connect_args={"check_same_thread": False},
                future=True,
                poolclass=NullPool,
            )
        return self._eng

    def _conn(self) -> Any:
        return self._engine().connect()


__all__ = [
    "SqlAlchemyConnector",
    "PostgresConnector",
    "MySQLConnector",
    "MSSQLConnector",
    "SQLiteConnector",
]
