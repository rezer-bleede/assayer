"""Tool: introspect a connected data source (schemas, tables, columns, row counts)."""

from __future__ import annotations

from typing import Any

from app.connectors.base import ConnectorError
from app.connectors.factory import build_connector
from app.core.logging import log
from app.schemas import ConnectionSpec


def introspect_source(
    spec: ConnectionSpec,
    schema: str | None = None,
    max_tables: int = 30,
) -> dict[str, Any]:
    # honour a schema declared on the connection spec (options.schema / schema_)
    schema = schema or _declared_schema(spec)
    conn = build_connector(spec)
    try:
        status = conn.test()
        if not status.ok:
            return {"ok": False, "error": status.message}
        data = conn.introspect(schema=schema, max_tables=max_tables)
        return {"ok": True, "connection": status.model_dump(mode="json"), **data}
    except ConnectorError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        log.warning("introspection failed", source=spec.name, error=str(exc))
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        conn.dispose()


def _declared_schema(spec: ConnectionSpec) -> str | None:
    options = spec.options or {}
    declared = options.get("schema")
    return declared if isinstance(declared, str) and declared else spec.schema_


def profile_table(
    spec: ConnectionSpec, schema: str | None, table: str, sample: int = 5
) -> dict[str, Any]:
    """Lightweight profiling used by the planner to infer sensible thresholds."""
    conn = build_connector(spec)
    try:
        cols = conn.describe_table(schema, table)
        keys = conn.primary_keys(schema, table)
        try:
            rc = conn.row_count(schema, table)
        except Exception:  # noqa: BLE001
            rc = None
        profiles: list[dict[str, Any]] = []
        for c in cols:
            entry: dict[str, Any] = {"name": c.name, "data_type": c.data_type, "nullable": c.nullable}
            try:
                q = (
                    f'SELECT COUNT(*) AS total, '
                    f'SUM(CASE WHEN "{c.name}" IS NULL THEN 1 ELSE 0 END) AS nulls, '
                    f'COUNT(DISTINCT "{c.name}") AS distinct_values '
                    f'FROM {conn.quote_table(schema, table)}'
                )
                row = conn.query(q).rows[0]
                total = float(row.get("total") or 0) or 1.0
                entry["null_rate"] = round(float(row.get("nulls") or 0) / total, 4)
                entry["distinct_ratio"] = round(float(row.get("distinct_values") or 0) / total, 4)
            except Exception as exc:  # noqa: BLE001
                entry["profile_error"] = str(exc)[:120]
            profiles.append(entry)
        return {
            "ok": True,
            "schema": schema,
            "table": table,
            "row_count": rc,
            "primary_keys": keys,
            "columns": profiles,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        conn.dispose()