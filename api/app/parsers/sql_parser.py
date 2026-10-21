"""Deterministic SQL analysis with sqlglot.

Extracts lineage (source -> target tables), joins, filters, aggregations and
derives transformation semantics used later to build the test plan.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

from app.core.logging import log
from app.schemas import EtlLanguage, LineageEdge, Transformation


def _table_name(node: exp.Expression) -> str | None:
    """Render a table reference as `db.schema.table`, without any alias."""
    try:
        if isinstance(node, exp.Table):
            parts = [p for p in (node.db, node.catalog) if p]
            parts.append(node.name)
            return ".".join(parts)
        return node.sql()
    except Exception:  # noqa: BLE001 - malformed identifier
        return None


def _column_list(node: exp.Expression | None) -> list[str]:
    if node is None:
        return []
    if isinstance(node, exp.Star):
        return ["*"]
    if isinstance(node, exp.Columns):
        return [e.name for e in node.expressions if hasattr(e, "name")]
    return []


def analyze_sql(
    code: str, label: str | None = None
) -> tuple[list[Transformation], list[LineageEdge], list[str], list[str], dict]:
    transformations: list[Transformation] = []
    edges: list[LineageEdge] = []
    sources: list[str] = []
    targets: list[str] = []
    extras: dict = {
        "joins": [],
        "filters": [],
        "aggregations": [],
        "statements": [],
        "ctes": [],
        "primary_source": [],
        "grain": {},
    }
    primary_source: str | None = None

    try:
        statements = sqlglot.parse(code)
    except Exception as exc:  # noqa: BLE001
        log.warning("sqlglot parse failed, falling back to regex", error=str(exc))
        edges = _regex_lineage(code)
        targets = _dedupe(_TARGET_RE.findall(code))
        sources = _dedupe(
            [s for s in _FROM_RE.findall(code) + _JOIN_RE.findall(code) if s not in targets]
        )
        return (
            _regex_fallback(code),
            edges,
            sources,
            targets,
            {"parse_error": str(exc), "joins": [], "filters": [], "aggregations": []},
        )

    statements = [s for s in statements if s is not None]
    extras["statements"] = len(statements)

    for stmt in statements:
        # CTEs -> named intermediate relations
        cte_names: dict[str, str] = {}
        for cte in stmt.find_all(exp.CTE):
            alias = cte.alias_or_name
            cte_names[alias.upper()] = f"cte:{alias}"
            extras["ctes"].append(alias)
            inner_tables = [
                _table_name(t) for t in cte.find_all(exp.Table) if t.name not in cte_names
            ]
            transformations.append(
                Transformation(
                    kind="cte",
                    description=f"CTE '{alias}' defined"
                    + (
                        f" from {', '.join(t for t in inner_tables[:3] if t)}"
                        if inner_tables
                        else ""
                    ),
                    source=inner_tables[0] if inner_tables else None,
                    target=f"cte:{alias}",
                    confidence=0.95,
                )
            )
            for t in inner_tables:
                if t:
                    edges.append(LineageEdge(source=t, target=f"cte:{alias}"))

        # target table: INSERT INTO / CREATE TABLE / MERGE / UPDATE
        target_table = None
        for kind in ("insert", "create", "merge", "update"):
            node = stmt.find(exp.Insert) if kind == "insert" else None
            if kind == "insert" and node is not None:
                target_table = _table_name(node.this)
            elif kind == "create":
                ct = stmt.find(exp.Create)
                if ct is not None and not isinstance(ct.this, exp.Schema):
                    target_table = _table_name(ct.this)
            elif kind == "merge":
                mg = stmt.find(exp.Merge)
                if mg is not None:
                    target_table = _table_name(mg.this)
            elif kind == "update":
                up = stmt.find(exp.Update)
                if up is not None:
                    target_table = _table_name(up.this)
            if target_table:
                break

        # source tables
        primary_source: str | None = None
        stmt_tables: list[str] = []
        for t in stmt.find_all(exp.Table):
            nm = t.name
            fqn = _table_name(t)
            if not nm:
                continue
            # CTEs are not physical tables
            if nm.upper() in cte_names:
                fqn = cte_names[nm.upper()]
                continue
            if t.name.lower() in {"values"}:
                continue
            if fqn and fqn not in stmt_tables:
                stmt_tables.append(fqn)
                # the first physical table walked that is NOT the write target
                if primary_source is None and fqn != target_table:
                    primary_source = fqn

        # skip the target itself when scanning for sources
        src_tables = [t for t in stmt_tables if t != target_table]

        # joins — sqlglot splits "left outer join" into side=LEFT, kind=OUTER
        # (unchanged) ...

        # filters
        # (unchanged) ...

        # aggregations
        # (unchanged) ...

        # select projections
        # (unchanged) ...

        # record the driving table for this statement
        if primary_source:
            if primary_source not in extras["primary_source"]:
                extras["primary_source"].append(primary_source)
            if target_table:
                extras["grain"].setdefault(target_table, primary_source)
                extras["grain"].setdefault(target_table.split(".")[-1].lower(), primary_source)
        sources.extend(src_tables)
        if target_table:
            targets.append(target_table)
        for s in src_tables:
            edges.append(LineageEdge(source=s, target=target_table or s))

        # joins — sqlglot splits "left outer join" into side=LEFT, kind=OUTER
        for join in stmt.find_all(exp.Join):
            on = join.args.get("on")
            side = (join.args.get("side") or "").upper()
            kind = (join.args.get("kind") or "").upper()
            how = " ".join(p for p in (side, kind) if p and p != "OUTER") or "inner"
            if side == "LEFT" and kind == "OUTER":
                how = "left"
            elif side == "RIGHT" and kind == "OUTER":
                how = "right"
            elif side == "FULL" and kind == "OUTER":
                how = "full"
            extras["joins"].append(
                {
                    "right_table": _table_name(join.this)
                    if isinstance(join.this, exp.Table)
                    else None,
                    "kind": how.lower(),
                    "on": on.sql()[:300] if on else None,
                    "using": [c.name for c in (join.args.get("using") or [])]
                    if join.args.get("using")
                    else [],
                }
            )
        for j in extras["joins"]:
            transformations.append(
                Transformation(
                    kind="join",
                    description=f"{j['kind'].upper()} join with {j.get('right_table') or 'subquery'}"
                    + (f" on {j['on']}" if j.get("on") else ""),
                    source=j.get("right_table"),
                    target=target_table,
                    confidence=0.9,
                )
            )

        # filters
        for where in stmt.find_all(exp.Where):
            cond = where.this.sql()[:300]
            extras["filters"].append({"condition": cond, "table": target_table})
            transformations.append(
                Transformation(
                    kind="filter",
                    description=f"Filter: {cond}",
                    target=target_table,
                    expression=cond,
                    confidence=0.85,
                )
            )

        # aggregations
        for agg in stmt.find_all(
            (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max, exp.Stddev, exp.Median)
        ):
            extras["aggregations"].append(
                {
                    "function": type(agg).__name__.lower(),
                    "expression": agg.sql()[:200],
                    "table": target_table,
                }
            )
        if extras["aggregations"]:
            fns = sorted({a["function"] for a in extras["aggregations"]})
            transformations.append(
                Transformation(
                    kind="aggregation",
                    description=f"Aggregation over {', '.join(fns)}",
                    target=target_table,
                    fields=[a["expression"] for a in extras["aggregations"][:6]],
                    confidence=0.9,
                )
            )

        # select projections (column lineage hints)
        select = stmt.find(exp.Select)
        if select is not None:
            projections = []
            for proj in select.expressions:
                alias = proj.alias_or_name
                if isinstance(proj, exp.Star):
                    projections.append("*")
                elif alias:
                    projections.append(f"{proj.sql()[:120]} AS {alias}")
                else:
                    projections.append(proj.sql()[:120])
            if projections and target_table:
                transformations.append(
                    Transformation(
                        kind="projection",
                        description=f"Project {len(projections)} column(s) into target",
                        source=", ".join(src_tables[:2]) or None,
                        target=target_table,
                        fields=projections[:25],
                        confidence=0.8,
                    )
                )
            # derived columns: cast / coalesce / case / arithmetic
            for derived in list(select.find_all(exp.Cast)) + list(select.find_all(exp.Case)):
                transformations.append(
                    Transformation(
                        kind="derivation",
                        description=f"Derived expression: {derived.sql()[:160]}",
                        target=target_table,
                        expression=derived.sql()[:200],
                        confidence=0.75,
                    )
                )

        # unions
        for _union in stmt.find_all(exp.Union):
            transformations.append(
                Transformation(
                    kind="union",
                    description="UNION of multiple inputs",
                    target=target_table,
                    confidence=0.8,
                )
            )

    if primary_source and primary_source not in extras["primary_source"]:
        extras["primary_source"].append(primary_source)
        if target_table:
            extras["grain"].setdefault(target_table, primary_source)
            extras["grain"].setdefault(target_table.split(".")[-1].lower(), primary_source)

    return transformations, edges, _dedupe(sources), _dedupe(targets), extras


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for i in items:
        if i and i not in seen:
            seen.add(i)
            out.append(i)
    return out


_CTE_RE = re.compile(r"(?:with|,)\s+(\w+)\s+as\s*\(", re.I)
_TARGET_RE = re.compile(
    r"(?:insert\s+into|create\s+(?:or\s+replace\s+)?table(?:\s+if\s+not\s+exists)?|merge\s+into)\s+"
    r"([\w.\"`\[\]]+)",
    re.I,
)
_FROM_RE = re.compile(r"\bfrom\s+([\w.\"`\[\]]+)", re.I)
_JOIN_RE = re.compile(r"\bjoin\s+([\w.\"`\[\]]+)", re.I)


def _regex_fallback(code: str) -> list[Transformation]:
    out: list[Transformation] = []
    targets = _TARGET_RE.findall(code)
    for t in targets:
        out.append(
            Transformation(kind="write", description=f"Writes into {t}", target=t, confidence=0.4)
        )
    for j in _JOIN_RE.findall(code):
        out.append(Transformation(kind="join", description=f"Joins {j}", source=j, confidence=0.4))
    return out


def _regex_lineage(code: str) -> list[LineageEdge]:
    targets = _TARGET_RE.findall(code)
    if not targets:
        return []
    tgt = targets[0]
    return [
        LineageEdge(source=s, target=tgt) for s in _FROM_RE.findall(code) + _JOIN_RE.findall(code)
    ]


def parse_sql(code: str, label: str | None = None):
    from app.schemas import EtlAnalysis

    tr, edges, srcs, tgts, extras = analyze_sql(code, label)
    risks = _risks(code)
    return EtlAnalysis(
        language=EtlLanguage.SQL,
        label=label,
        transformations=tr,
        lineage=edges,
        sources=srcs,
        targets=tgts,
        joins=extras.get("joins", []),
        filters=extras.get("filters", []),
        aggregations=extras.get("aggregations", []),
        primary_sources=[t for t in extras.get("primary_source", []) if t],
        grain=extras.get("grain", {}),
        risks=risks,
        raw_code=code,
    )


def _risks(code: str) -> list[str]:
    risks: list[str] = []
    low = code.lower()
    if "select *" in low or "select  *" in low:
        risks.append("SELECT * used: column drift can silently change downstream contracts")
    if "inner join" in low and "where" not in low:
        risks.append("INNER JOIN may drop rows from the source; verify row-count parity")
    if "left join" in low:
        risks.append("LEFT JOIN: expect NULLs in target columns sourced from the right table")
    if "union all" in low and "distinct" not in low:
        risks.append("UNION ALL can duplicate keys; check uniqueness on target")
    if re.search(r"delete\s+from|truncate\s+table|drop\s+table", low):
        risks.append("Destructive DDL/DML detected in the script")
    if "limit" in low:
        risks.append("LIMIT in transformation can create partial datasets (check volume)")
    if "coalesce" in low:
        risks.append("COALESCE hides NULLs upstream; validate source null rates")
    if "cast" in low and ("as int" in low or "as integer" in low):
        risks.append("Casting to INT can fail or nullify on overflow/strings")
    if "date(" in low or "trunc(" in low:
        risks.append("Date truncation applied: check grain/timezone consistency")
    if not risks:
        risks.append("No obvious structural risk detected in the SQL")
    return risks
