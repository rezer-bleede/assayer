"""Connector factory: maps a ConnectionSpec to a live, read-only connector."""

from __future__ import annotations

from typing import Any

from app.connectors.base import Connector, ConnectorError
from app.schemas import ConnectionSpec, SourceType

_REGISTRY: dict[SourceType, str] = {
    # Registered for the supported-source catalogue; browser targets use
    # build_browser(), never the SQL Connector interface.
    SourceType.WEB: "app.connectors.browser:BrowserClient",
    SourceType.POSTGRES: "app.connectors.sqlalchemy_connector:PostgresConnector",
    SourceType.MYSQL: "app.connectors.sqlalchemy_connector:MySQLConnector",
    SourceType.MSSQL: "app.connectors.sqlalchemy_connector:MSSQLConnector",
    SourceType.CLICKHOUSE: "app.connectors.clickhouse:ClickHouseConnector",
    SourceType.SQLITE: "app.connectors.sqlalchemy_connector:SQLiteConnector",
    SourceType.SNOWFLAKE: "app.connectors.snowflake:SnowflakeConnector",
    SourceType.BIGQUERY: "app.connectors.bigquery:BigQueryConnector",
    SourceType.DATABRICKS: "app.connectors.databricks:DatabricksConnector",
    SourceType.LOCAL_CSV: "app.connectors.files:LocalFilesConnector",
    SourceType.S3_CSV: "app.connectors.files:S3FilesConnector",
}

SUPPORTED_SOURCES: list[dict[str, Any]] = [
    {"type": str(s), "label": str(s).replace("_", " ").title()} for s in SourceType
]


def _load(dotted: str) -> type[Connector]:
    import importlib

    mod_name, cls_name = dotted.split(":")
    mod = importlib.import_module(mod_name)
    cls: type[Connector] = getattr(mod, cls_name)
    return cls


def build_connector(spec: ConnectionSpec) -> Connector:
    if spec.type == SourceType.WEB:
        raise ConnectorError("web targets use build_browser(), not a SQL connector")
    dotted = _REGISTRY.get(spec.type)
    if dotted is None:
        raise ConnectorError(f"unsupported source type: {spec.type}")
    cls = _load(dotted)
    try:
        return cls(spec)
    except ConnectorError:
        raise
    except ModuleNotFoundError as exc:
        raise ConnectorError(
            f"driver for '{spec.type}' is not installed in this image ({exc.name}). "
            f"Add the matching extra to api/pyproject.toml and rebuild."
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise ConnectorError(f"cannot build connector for '{spec.type}': {exc}") from exc


def build_browser(spec: ConnectionSpec):
    if spec.type != SourceType.WEB:
        raise ConnectorError("browser target must have type 'web'")
    from app.connectors.browser import BrowserClient

    return BrowserClient(spec)
__all__ = ["build_connector", "build_browser", "SUPPORTED_SOURCES", "Connector", "ConnectorError"]
