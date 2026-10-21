"""Compile a CheckSpec into a safe, read-only SQL probe.

All probes are single aggregate SELECTs — no DDL/DML, no multi-statement,
enforced with a SELECT-only guard before execution.
"""

from __future__ import annotations

from app.connectors.base import Connector
from app.schemas import CheckSpec, Severity


class UnsafeSQLError(ValueError):
    pass


# Checking a column implies naming one: interpolation of a missing column
# produced literal SQL like `WHEN None IS NULL` — readable nonsense that
# failed late with a cryptic error. Public so the plan builder can drop
# such checks before they ever reach a POST /runs call.
NEEDS_COLUMN = (
    "null_rate",
    "unique",
    "duplicate",
    "referential",
    "freshness",
    "min_max",
    "range",
    "pattern",
    "distribution",
)


_FORBIDDEN = (
    "insert",
    "update",
    "delete",
    "drop",
    "truncate",
    "alter",
    "create",
    "grant",
    "revoke",
    "merge",
    "replace",
    "attach",
    "copy",
    "vacuum",
)


def assert_readonly(sql: str) -> None:
    low = " ".join(sql.lower().split())
    if not low.startswith(("select", "with", "show", "describe", "pragma")):
        raise UnsafeSQLError("only SELECT/WITH queries are allowed")
    if ";" in low.rstrip(";"):
        raise UnsafeSQLError("multiple statements are not allowed")
    for word in _FORBIDDEN:
        if f" {word} " in f" {low} ":
            raise UnsafeSQLError(f"forbidden keyword in query: {word}")


def _n(v: float | str | None, default: float = 0.0) -> float:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _literal(value: float | str | None) -> str:
    """Render a bound safely: numbers bare, strings single-quoted and escaped."""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def compile_check(check: CheckSpec, conn: Connector) -> tuple[str, dict]:
    """Return (sql, params). `expected` is computed by the runner from the row."""
    t = conn.quote_table(check.table.schema_, check.table.name)
    col = f'"{check.column}"' if check.column else None
    kind = str(check.kind)

    if col is None and kind in NEEDS_COLUMN:
        raise UnsafeSQLError(f"a {kind} check must name a column")

    match kind:
        case "null_rate":
            sql = (
                f"SELECT COUNT(*) AS total, "
                f"SUM(CASE WHEN {col} IS NULL THEN 1 ELSE 0 END) AS nulls "
                f"FROM {t}"
            )
            return sql, {}

        case "unique":
            sql = f"SELECT COUNT(*) AS total, COUNT(DISTINCT {col}) AS distinct_values FROM {t}"
            return sql, {}

        case "duplicate":
            sql = (
                f"SELECT COALESCE(SUM(c - 1), 0) AS extra_rows, COALESCE(MAX(c), 0) AS max_dupes "
                f"FROM (SELECT COUNT(*) AS c FROM {t} GROUP BY {col} HAVING COUNT(*) > 1) x"
            )
            return sql, {}

        case "row_count" | "volume":
            sql = f"SELECT COUNT(*) AS row_count FROM {t}"
            return sql, {}

        case "referential":
            parent_schema = check.params.get("parent_schema")
            parent_table = check.params.get("parent_table")
            parent_col = check.params.get("parent_column", check.column)
            pt = f'"{parent_schema}"."{parent_table}"' if parent_schema else f'"{parent_table}"'
            sql = (
                f"SELECT COUNT(*) AS total, "
                f'SUM(CASE WHEN p."{parent_col}" IS NULL THEN 1 ELSE 0 END) AS orphans '
                f'FROM {t} c LEFT JOIN {pt} p ON c.{col} = p."{parent_col}"'
            )
            return sql, {}

        case "freshness":
            sql = f"SELECT COUNT(*) AS total, MAX({col}) AS latest, MIN({col}) AS earliest FROM {t}"
            return sql, {}

        case "min_max":
            col2 = check.column
            sql = f'SELECT MIN("{col2}") AS min_value, MAX("{col2}") AS max_value FROM {t}'
            return sql, {}

        case "range":
            # Only emit the bounds that were actually specified; an open-ended
            # range (e.g. "amount >= 0") must not render as "> None".
            clauses = []
            if check.min_value is not None:
                clauses.append(f'"{check.column}" < {_literal(check.min_value)}')
            if check.max_value is not None:
                clauses.append(f'"{check.column}" > {_literal(check.max_value)}')
            if not clauses:
                raise UnsafeSQLError("range check needs at least one of min_value/max_value")
            cond = " OR ".join(clauses)
            sql = (
                f"SELECT COUNT(*) AS total, "
                f"SUM(CASE WHEN {cond} THEN 1 ELSE 0 END) AS violations "
                f"FROM {t}"
            )
            return sql, {}

        case "pattern":
            if not check.regex:
                raise UnsafeSQLError("pattern check has no regex")
            mismatch = conn.regex_mismatch(f'"{check.column}"', check.regex)
            sql = (
                f"SELECT COUNT(*) AS total, "
                f"SUM(CASE WHEN {mismatch} THEN 1 ELSE 0 END) AS violations "
                f"FROM {t}"
            )
            return sql, {}

        case "distribution":
            col2 = check.column
            sql = (
                f'SELECT COUNT(*) AS total, COUNT("{col2}") AS non_null, '
                f'COUNT(DISTINCT "{col2}") AS distinct_values, '
                f'MIN("{col2}") AS min_value, MAX("{col2}") AS max_value FROM {t}'
            )
            return sql, {}

        case "schema":
            return "", {}

        case "custom_sql":
            if not check.sql:
                raise UnsafeSQLError("custom_sql check has no sql")
            return check.sql, check.params

    raise UnsafeSQLError(f"unsupported check kind: {kind}")


def sample_failure_sql(check: CheckSpec, conn: Connector) -> str:
    """SQL that returns a few offending rows for the UI sample table."""
    t = conn.quote_table(check.table.schema_, check.table.name)
    col = check.column
    kind = str(check.kind)
    match kind:
        case "null_rate":
            return f'SELECT * FROM {t} WHERE "{col}" IS NULL'
        case "unique" | "duplicate":
            keys = check.params.get("keys") or ([col] if col else [])
            grp = ", ".join(f'"{k}"' for k in keys)
            return (
                f"SELECT * FROM {t} WHERE ({grp}) IN "
                f"(SELECT {grp} FROM {t} GROUP BY {grp} HAVING COUNT(*) > 1)"
            )
        case "referential":
            p_schema = check.params.get("parent_schema")
            p_table = check.params.get("parent_table")
            p_col = check.params.get("parent_column", col)
            pt = f'"{p_schema}"."{p_table}"' if p_schema else f'"{p_table}"'
            return (
                f'SELECT c.* FROM {t} c LEFT JOIN {pt} p ON c."{col}" = p."{p_col}" '
                f'WHERE p."{p_col}" IS NULL'
            )
        case "range":
            clauses = []
            if check.min_value is not None:
                clauses.append(f'"{col}" < {_literal(check.min_value)}')
            if check.max_value is not None:
                clauses.append(f'"{col}" > {_literal(check.max_value)}')
            return f"SELECT * FROM {t} WHERE " + " OR ".join(clauses)
        case "pattern":
            mismatch = conn.regex_mismatch(f'"{col}"', check.regex or "")
            return f"SELECT * FROM {t} WHERE {mismatch}"
        case "freshness":
            return f'SELECT * FROM {t} ORDER BY "{col or "_ingest_ts"}" DESC'
    return f"SELECT * FROM {t}"


def severity_weight(sev: Severity) -> float:
    return {
        Severity.BLOCKER: 4.0,
        Severity.CRITICAL: 2.0,
        Severity.WARNING: 1.0,
        Severity.INFO: 0.25,
    }.get(sev, 1.0)
