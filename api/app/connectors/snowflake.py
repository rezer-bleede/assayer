"""Snowflake connector (SQLAlchemy dialect, installed via the `snowflake` extra)."""

from __future__ import annotations

from app.connectors.sqlalchemy_connector import SqlAlchemyConnector
from app.schemas import SourceType


class SnowflakeConnector(SqlAlchemyConnector):
    source_type = SourceType.SNOWFLAKE

    schema_query = "SELECT SCHEMA_NAME FROM information_schema.schemata ORDER BY 1"
    table_query = (
        "SELECT TABLE_NAME FROM information_schema.tables "
        "WHERE TABLE_SCHEMA = :schema AND TABLE_TYPE IN ('BASE TABLE','VIEW') ORDER BY TABLE_NAME"
    )
    columns_query = (
        "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE FROM information_schema.columns "
        "WHERE TABLE_SCHEMA = :schema AND TABLE_NAME = :table ORDER BY ORDINAL_POSITION"
    )

    @property
    def default_port(self) -> int:
        return 443

    def _url(self) -> str:
        from urllib.parse import quote_plus

        s = self.spec
        account = s.account or (s.host or "").split(".")[0]
        pwd = quote_plus(s.password.get_secret_value()) if s.password else ""
        user = quote_plus(s.username or "")
        params = [f"warehouse={s.warehouse or 'COMPUTE_WH'}"]
        if s.schema_:
            params.append(f"schema={s.schema_}")
        if s.role:
            params.append(f"role={s.role}")
        return f"snowflake://{user}:{pwd}@{account}/{s.database or ''}?{'&'.join(params)}"

    def _conn(self) -> object:
        return self._engine().connect()