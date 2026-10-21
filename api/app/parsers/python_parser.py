"""Python ETL analysis using the stdlib AST.

Covers pandas / PySpark / SQLAlchemy idioms: `read_sql`, `read_csv`,
`merge`, `join`, `groupby().agg()`, `fillna`, `to_sql`, `to_parquet`, ...
Lineage is resolved through variable assignments, so a chain like

    raw = pd.read_sql(...)
    joined = raw.merge(ref, on="customer_id")
    agg = joined.groupby("customer_id").agg(amount=("amount", "sum"))
    agg.to_sql("customer_totals", engine)

is reported as `raw.orders -> customer_totals`, not as a flat list of calls.
"""

from __future__ import annotations

import ast
import re

from app.schemas import EtlLanguage, LineageEdge, Transformation

# read functions: final method name -> transformation kind
_READERS = {
    "read_sql": "sql_source",
    "read_sql_query": "sql_source",
    "read_sql_table": "sql_source",
    "read_csv": "csv_source",
    "read_parquet": "parquet_source",
    "read_json": "json_source",
    "table": "table_source",
    "sql": "sql_source",
    "text": "sql_source",
}

# write functions (pandas, Spark, dask)
_SINKS = {
    "to_sql",
    "to_parquet",
    "to_csv",
    "to_json",
    "save",
    "write",
    "saveAsTable",
    "save_as_table",
    "insertInto",
    "insert_into",
    "to_delta",
    "to_database",
    "createOrReplaceTempView",
}

# dataframe operations that carry data forward
_PASSTHROUGH = {
    "merge",
    "join",
    "assign",
    "query",
    "filter",
    "with_column",
    "select",
    "groupby",
    "agg",
    "reset_index",
    "rename",
    "drop_duplicates",
    "dropna",
    "fillna",
    "astype",
    "sort_values",
    "head",
    "to_frame",
    "melt",
    "explode",
}

# aggregation entry points
_AGGS = {"groupby", "agg", "sum", "mean", "count", "nunique", "median", "std", "var"}

# operations that silently change the data (and therefore the grain / completeness)
_SANITISERS = {"fillna", "dropna", "drop_duplicates", "astype", "sample"}

_SANITISE_RISK = {
    "fillna": "fillna imputes missing values; null-rate checks pass by construction",
    "dropna": "dropna removes incomplete rows; verify source vs target row counts",
    "drop_duplicates": "drop_duplicates hides genuine duplicates in the source",
    "astype": "astype coerces types; values may silently null out",
    "sample": "sampling produces partial data; volume checks may fail spuriously",
}

_SQL_TABLE_RE = re.compile(r"(?:from|join|into|update)\s+([\w.\"`\[\]]+)", re.I)


def _call_name(node: ast.Call) -> str:
    parts: list[str] = []
    cur: ast.expr = node.func
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return ".".join(reversed(parts))


def _final_name(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def _literal(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _root_var(node: ast.expr) -> str:
    """Walk a chained call down to the variable it starts from.

    `df.write.mode(..).saveAsTable(..)` -> `df`
    `merged.groupby(..).agg(..).reset_index()` -> `merged`
    """
    cur: ast.expr = node
    for _ in range(64):  # bound against pathological/cyclic trees
        if isinstance(cur, ast.Name):
            return cur.id
        if isinstance(cur, (ast.Attribute, ast.Subscript, ast.Starred, ast.Await)):
            cur = cur.value
            continue
        if isinstance(cur, ast.Call):
            if (recv := _receiver(cur)) is not None:
                cur = recv
                continue
            if cur.args:
                cur = cur.args[0]
                continue
            return ""
        return ""
    return ""


def _df_name(node: ast.expr) -> str:
    """Resolve an expression down to the dataframe variable it references.

    Handles chains (`a.merge(b).groupby(...)`), subscripts (`customers[["id"]]`)
    and keyword subscripts (`customers[on=...]`).
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _root_var(node) or _final_name(_call_name(node))
    if isinstance(node, ast.Subscript):
        return _df_name(node.value)
    if isinstance(node, ast.Starred):
        return _df_name(node.value)
    if isinstance(node, ast.Await):
        return _df_name(node.value)
    if isinstance(node, ast.IfExp):
        return _df_name(node.body)
    return ""


def _receiver(call: ast.Call) -> ast.expr | None:
    """The object a method is called on, for `a.merge(...)` it is `a`."""
    return call.func.value if isinstance(call.func, ast.Attribute) else None


def _upstream_tables(call: ast.Call, vars_: dict[str, _Var]) -> list[str]:
    """Every table feeding a call, from its receiver and from all arguments."""
    out: list[str] = []
    recv = _receiver(call)
    if recv is not None:
        out.extend(vars_.get(_df_name(recv), _Var("", [])).tables)
    for arg in list(call.args) + [k.value for k in call.keywords]:
        name = _df_name(arg)
        if name:
            out.extend(vars_.get(name, _Var("", [])).tables)
    return _dedupe(out)


def _first_string(node: ast.Call, keywords: tuple[str, ...] = ()) -> str | None:
    for kw in node.keywords:
        if kw.arg in keywords or (keywords and kw.arg is None):
            lit = _literal(kw.value)
            if lit:
                return lit
    if node.args:
        return _literal(node.args[0])
    return None


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for i in items:
        if i and i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _extract_tables(sql: str | None) -> list[str]:
    if not sql:
        return []
    return _dedupe(_SQL_TABLE_RE.findall(sql))


def _target_name(literal: str | None) -> str | None:
    """A sink target is either a bare table name or an `INSERT INTO x` statement."""
    if not literal:
        return None
    if " " in literal.strip():
        tables = _extract_tables(literal)
        return tables[0] if tables else None
    return literal.strip().strip("\"'`")


class _Var:
    """A dataframe variable and the physical tables it derives from."""

    __slots__ = ("name", "tables")

    def __init__(self, name: str, tables: list[str]) -> None:
        self.name = name
        self.tables = tables


def analyze_python(
    code: str,
) -> tuple[list[Transformation], list[LineageEdge], list[str], list[str], dict]:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return (
            [
                Transformation(
                    kind="parse_error", description=f"Syntax error: {exc.msg}", confidence=1.0
                )
            ],
            [],
            [],
            [],
            {"parse_error": exc.msg, "line": exc.lineno},
        )

    transformations: list[Transformation] = []
    edges: list[LineageEdge] = []
    sources: list[str] = []
    targets: list[str] = []
    joins: list[dict] = []
    aggregations: list[dict] = []
    filters: list[dict] = []

    vars_: dict[str, _Var] = {}  # var -> tables it derives from
    sinks: list[tuple[str, list[str]]] = []  # (target table, upstream tables)

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            call = node.value
            name = _call_name(call)
            final = _final_name(name)
            bound = _df_name(node.targets[0]) if node.targets else None
            if not bound:
                continue

            if final in _READERS:
                kind = _READERS[final]
                src = _first_string(call, ("sql", "query", "table_name", "name")) or _literal(
                    call.args[-1] if call.args else None
                )
                tables = _extract_tables(src) if kind == "sql_source" else ([src] if src else [])
                vars_[bound] = _Var(bound, _dedupe(tables))
                sources.extend(vars_[bound].tables)
                transformations.append(
                    Transformation(
                        kind=kind,
                        description=f"{name}(" + (f"{src!r}" if src else "...") + ")",
                        source=src,
                        fields=[bound],
                        confidence=0.9,
                    )
                )
            elif final in _PASSTHROUGH:
                upstream = _upstream_tables(call, vars_)
                vars_[bound] = _Var(bound, upstream)
                if final in {"merge", "join"}:
                    recv = _receiver(call)
                    right = (
                        _df_name(call.args[0])
                        if call.args
                        else _df_name(recv)
                        if recv is not None
                        else ""
                    ) or "?"
                    on_kw = next((k for k in call.keywords if k.arg == "on"), None)
                    how_kw = next((k for k in call.keywords if k.arg == "how"), None)
                    joins.append(
                        {
                            "kind": "dataframe",
                            "right_table": right,
                            "method": final,
                            "on": _literal(on_kw.value) if on_kw else None,
                            "how": _literal(how_kw.value) if how_kw else "inner",
                            "using": [],
                        }
                    )
                    transformations.append(
                        Transformation(
                            kind="join",
                            description=f"dataframe .{final}() with {right}"
                            + (f" on {on_kw and _literal(on_kw.value)}" if on_kw else ""),
                            source=right,
                            target=bound,
                            fields=sorted(set(upstream)),
                            confidence=0.85,
                        )
                    )
                else:
                    transformations.append(
                        Transformation(
                            kind="transform",
                            description=f".{final}() into `{bound}`",
                            target=bound,
                            fields=sorted(set(upstream)),
                            confidence=0.7,
                        )
                    )

            elif final in _AGGS:
                aggregations.append(
                    {
                        "function": final,
                        "table": bound,
                        "from": sorted(set(_upstream_tables(call, vars_))),
                    }
                )
                vars_.setdefault(bound, _Var(bound, []))

        if isinstance(node, ast.Call):
            name = _call_name(node)
            final = _final_name(name)
            recv = _receiver(node)

            if final in _AGGS:
                aggregations.append(
                    {
                        "function": final,
                        "table": _df_name(recv) if recv is not None else "",
                        "from": sorted(set(_upstream_tables(node, vars_))),
                    }
                )

            if final in _SINKS:
                target = _first_string(node, ("name", "table", "path")) or _literal(
                    node.args[0] if node.args else None
                )
                table = _target_name(target)
                bound = _df_name(recv) if recv is not None else None
                upstream = list(vars_.get(bound, _Var("", [])).tables) if bound else []
                sinks.append((table or target or "dataframe", _dedupe(upstream)))
                transformations.append(
                    Transformation(
                        kind="write",
                        description=f".{final}() -> {table or target}",
                        source=bound,
                        target=table or target,
                        confidence=0.95,
                    )
                )
            elif final in _SANITISERS:
                bound = _df_name(recv) if recv is not None else None
                risk = _SANITISE_RISK[final]
                transformations.append(
                    Transformation(
                        kind="sanitise",
                        description=f".{final}() — {risk}",
                        source=bound,
                        confidence=0.8,
                    )
                )
                if final == "fillna":
                    filters.append({"call": final, "var": bound})
            elif "sql" in final and recv is not None:
                stmt = _literal(node.args[0]) if node.args else ""
                if "insert" in stmt.lower() or "create" in stmt.lower():
                    tbl = _extract_tables(stmt)
                    if tbl:
                        sinks.append((tbl[0], list(vars_.get(_df_name(recv), _Var("", [])).tables)))
                        transformations.append(
                            Transformation(
                                kind="write", description=f"SQL write into {tbl[0]}", target=tbl[0]
                            )
                        )

    # resolve lineage: every upstream table of a sink flows into that sink
    for table, upstream in sinks:
        targets.append(table)
        for u in upstream:
            edges.append(LineageEdge(source=u, target=table))

    if aggregations:
        fns = sorted({a["function"] for a in aggregations})
        transformations.append(
            Transformation(
                kind="aggregation",
                description=f"Aggregation over {', '.join(fns)}",
                fields=[a["table"] for a in aggregations],
                confidence=0.9,
            )
        )

    return (
        transformations,
        edges,
        _dedupe(sources),
        _dedupe(targets),
        {"joins": joins, "filters": filters, "aggregations": aggregations},
    )


def parse_python(code: str, label: str | None = None):
    from app.schemas import EtlAnalysis

    tr, edges, srcs, tgts, extras = analyze_python(code)
    return EtlAnalysis(
        language=EtlLanguage.PYTHON,
        label=label,
        transformations=tr,
        lineage=edges,
        sources=srcs,
        targets=tgts,
        joins=extras.get("joins", []),
        filters=extras.get("filters", []),
        aggregations=extras.get("aggregations", []),
        risks=_risks(code),
        raw_code=code,
    )


def _risks(code: str) -> list[str]:
    risks: list[str] = []
    low = code.lower()
    if "drop_duplicates" in low:
        risks.append(
            "drop_duplicates hides genuine duplicate source rows; verify uniqueness on target"
        )
    if "fillna" in low:
        risks.append("fillna imputes missing values; null-rate checks will pass by construction")
    if ".merge(" in low:
        risks.append("DataFrame.merge defaults to an inner join: confirm no rows are lost")
    if "how='left'" in low or 'how="left"' in low:
        risks.append("LEFT merge: expect NULLs in newly introduced columns")
    if "astype" in low:
        risks.append("astype coerces types; validate values did not silently null out")
    if "dropna" in low:
        risks.append("dropna removes incomplete rows; compare source vs target row counts")
    if "to_sql" in low and re.search(r"if_exists\s*=\s*['\"]append['\"]", low):
        risks.append("append mode is not idempotent: reruns will duplicate data")
    if "upsert" in low or "on_conflict" in low:
        risks.append("upsert semantics detected — verify the conflict key is unique")
    if "drop_duplicates" not in low and "groupby" not in low and "merge" not in low:
        risks.append(
            "no aggregation or join detected: target grain may not be what downstream expects"
        )
    if not risks:
        risks.append("No obvious structural risk detected in the Python script")
    return _dedupe(risks)
