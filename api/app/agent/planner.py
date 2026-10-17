"""Deterministic test-plan generation.

This is the default path: it needs no LLM and produces auditable checks derived
purely from the ETL parse + live schema/profile evidence. The LLM layer only
*augments* this plan (see agent/plan_builder.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.config import settings
from app.core.logging import log
from app.schemas import (
    CheckKind,
    CheckSpec,
    EtlAnalysis,
    Severity,
    TableRef,
)


@dataclass(slots=True)
class SourceBinding:
    """A connected source that we mapped onto a table mentioned in the ETL."""

    source_id: str
    name: str
    schema: str | None
    table: str
    is_target: bool = False
    row_count: int | None = None
    primary_keys: list[str] | None = None
    columns: list[str] | None = None


_TYPE_HINTS = {
    "int": (CheckKind.RANGE, Severity.WARNING),
    "numeric": (CheckKind.RANGE, Severity.WARNING),
    "decimal": (CheckKind.RANGE, Severity.WARNING),
    "double": (CheckKind.RANGE, Severity.WARNING),
    "float": (CheckKind.RANGE, Severity.WARNING),
    "bool": (CheckKind.RANGE, Severity.WARNING),
    "date": (CheckKind.FRESHNESS, Severity.INFO),
    "timestamp": (CheckKind.FRESHNESS, Severity.INFO),
    "string": (CheckKind.MIN_MAX, Severity.INFO),
    "text": (CheckKind.MIN_MAX, Severity.INFO),
}

_KEY_RE = re.compile(r"(^id$|_id$|^id_|_key$|key$|^pk)", re.I)
_EMAIL_RE = re.compile(r"email", re.I)
_AMOUNT_RE = re.compile(r"amount|price|total|balance|revenue|qty|quantity", re.I)
_PCT_RE = re.compile(r"rate|pct|percent|ratio", re.I)
_STATUS_RE = re.compile(r"status|state|type|category|segment", re.I)
_CURRENCY_RE = re.compile(r"currenc|ccy", re.I)


def _norm(name: str) -> str:
    return re.sub(r"[`\[\]\"']", "", name).strip()


def _bare(name: str) -> str:
    n = _norm(name)
    return n.split(".")[-1].lower()


def _schema_of(name: str) -> str | None:
    n = _norm(name)
    parts = n.split(".")
    return parts[0] if len(parts) > 1 else None


def bind_sources(
    analysis: EtlAnalysis,
    connections: dict[str, object],
    introspected: dict[str, list[dict]],
) -> list[SourceBinding]:
    """Map ETL table references onto connected sources by name (target wins).

    A reference like `raw.orders` binds to the introspected table whose bare name
    is `orders`; the schema always comes from the *matched table*, not from the
    connection default, so `raw.orders` never resolves to `public.orders`.
    """
    targets = {_bare(t) for t in analysis.targets}
    sources_refs = {_bare(s) for s in analysis.sources}
    bindings: list[SourceBinding] = []
    used: set[tuple[str, str]] = set()

    # 1. explicit user intent from the connection options.
    #    `options.tables` is set when the user answered a datasource question,
    #    which makes binding deterministic instead of name-fuzzy.
    for cid, spec in connections.items():
        options = getattr(spec, "options", None) or {}
        declared = options.get("tables") or (
            [options["table"]] if options.get("table") else []
        )
        if not declared:
            continue
        for raw in declared:
            bare = _bare(raw)
            meta = _match_meta(introspected.get(cid, []), bare)
            if meta is None:
                # never substitute a similar-looking table: audit nothing and say so
                continue
            bindings.append(
                SourceBinding(
                    source_id=cid,
                    name=getattr(spec, "name", cid),
                    schema=meta.get("schema")
                    or options.get("schema")
                    or getattr(spec, "schema_", None),
                    table=_bare(meta.get("name", bare)),
                    is_target=_role_is_target(options, bare, targets),
                    row_count=meta.get("row_count"),
                    primary_keys=meta.get("primary_keys"),
                    columns=[c.get("name") for c in meta.get("columns", [])] or None,
                )
            )
            used.add((cid, bare))

    # 2. inferred by name matching against every connected source
    for cid, spec in connections.items():
        tables = introspected.get(cid, [])
        for ref in targets | sources_refs:
            if any(b.source_id == cid and b.table == ref for b in bindings):
                continue
            meta = _match_meta(tables, ref)
            if meta is None:
                continue
            bindings.append(
                SourceBinding(
                    source_id=cid,
                    name=getattr(spec, "name", cid),
                    schema=meta.get("schema") or getattr(spec, "schema_", None),
                    table=_bare(meta.get("name", ref)),
                    is_target=ref in targets and ref not in sources_refs,
                    row_count=meta.get("row_count"),
                    primary_keys=meta.get("primary_keys"),
                    columns=[c.get("name") for c in meta.get("columns", [])] or None,
                )
            )

    return bindings


def _role_is_target(options: dict, bare: str, targets: set[str]) -> bool:
    """Did the user declare this table as a write target?"""
    declared = options.get("target_tables")
    if isinstance(declared, (list, tuple)) and declared:
        return bare in {_bare(t) for t in declared}
    if options.get("role") == "target":
        return True
    return bare in targets


def _match_meta(tables: list[dict], ref: str) -> dict | None:
    """Find the introspected table with this exact bare name.

    Deliberately not fuzzy: `stg_customers` must never resolve to `customers`,
    or we would query one table and report the findings against another.
    """
    for t in tables:
        if t.get("name") and _bare(t["name"]) == ref:
            return t
    return None


def find_unresolved(
    analysis: EtlAnalysis | None,
    bindings: list[SourceBinding],
) -> list[str]:
    """ETL tables that no connected source could provide — reported, never guessed."""
    if analysis is None:
        return []
    have = {b.table for b in bindings}
    wanted = {_bare(t) for t in [*analysis.sources, *analysis.targets]}
    return sorted(wanted - have)


def _columns(binding: SourceBinding) -> list[str]:
    return [c for c in (binding.columns or []) if c]


def _table_ref(b: SourceBinding) -> TableRef:
    return TableRef(source_id=b.source_id, schema=b.schema, name=b.table)


def plan_checks(
    analysis: EtlAnalysis | None,
    bindings: list[SourceBinding],
    profile: dict[str, dict] | None = None,
) -> list[CheckSpec]:
    """Deterministic baseline checks. Ordered by blast radius."""
    checks: list[CheckSpec] = []
    profile = profile or {}

    for b in bindings:
        ref = _table_ref(b)
        prof = profile.get(f"{b.source_id}|{b.table}", {})
        cols = _columns(b)
        col_prof = {c["name"]: c for c in prof.get("columns", []) if isinstance(c, dict) and "name" in c}

        # --- 1. existence / shape -------------------------------------- #
        checks.append(
            CheckSpec(
                name=f"[{b.table}] table is queryable",
                kind=CheckKind.ROW_COUNT,
                table=ref,
                severity=Severity.BLOCKER,
                rationale="Pipeline is a no-op if the target cannot be read",
                params={"expected_row_count": b.row_count} if b.row_count else {},
                tags=["baseline"],
            )
        )

        if b.is_target and b.row_count is not None:
            checks.append(
                CheckSpec(
                    name=f"[{b.table}] is not empty",
                    kind=CheckKind.VOLUME,
                    table=ref,
                    severity=Severity.BLOCKER,
                    threshold=1,
                    rationale="An empty target table usually means the ETL silently produced nothing",
                    params={"min_rows": 1},
                    tags=["baseline", "volume"],
                )
            )

        # --- 2. primary keys / uniqueness ------------------------------- #
        pk_cols = [c for c in (b.primary_keys or []) if c]
        if pk_cols:
            checks.append(
                CheckSpec(
                    name=f"[{b.table}] primary key is unique",
                    kind=CheckKind.UNIQUE,
                    table=ref,
                    column=pk_cols[0] if len(pk_cols) == 1 else None,
                    severity=Severity.BLOCKER,
                    rationale=f"Declared key ({', '.join(pk_cols)}) must not duplicate after transformation",
                    params={"keys": pk_cols},
                    tags=["keys"],
                )
            )
        else:
            for c in cols:
                if _KEY_RE.search(c):
                    checks.append(
                        CheckSpec(
                            name=f"[{b.table}.{c}] key-like column is unique",
                            kind=CheckKind.UNIQUE,
                            table=ref,
                            column=c,
                            severity=Severity.CRITICAL,
                            rationale="Column name suggests an identifier; duplicates corrupt joins downstream",
                            tags=["keys", "inferred"],
                        )
                    )
                    break

        # --- 3. join keys match across source/target -------------------- #
        for edge in (analysis.lineage if analysis else []):
            if _bare(edge.target) != b.table or _bare(edge.source) == b.table:
                continue
            child_col, parent_col = _join_key(analysis, b.table, _bare(edge.source))
            if not child_col or not parent_col:
                continue
            checks.append(
                CheckSpec(
                    name=f"[{b.table}.{child_col}] joins to '{edge.source}' on "
                         f"{parent_col}",
                    kind=CheckKind.REFERENTIAL,
                    table=ref,
                    column=child_col,
                    severity=Severity.CRITICAL,
                    rationale=(
                        f"Every {_bare(edge.source)} row referenced by "
                        f"{b.table}.{child_col} must exist in {_bare(edge.source)}.{parent_col}"
                    ),
                    params={
                        "parent_table": _bare(edge.source),
                        "parent_column": parent_col,
                        "parent_schema": _schema_of(edge.source),
                    },
                    tags=["lineage", "referential"],
                )
            )
            break

        # --- 4. nullability -------------------------------------------- #
        for c in cols:
            meta = col_prof.get(c, {})
            null_rate = meta.get("null_rate")
            dtype = str(meta.get("data_type", "")).lower()
            is_key = _KEY_RE.search(c) is not None
            severity = Severity.CRITICAL if is_key else Severity.WARNING
            if not is_key and (dtype.startswith(("int", "numeric", "decimal", "double", "float"))):
                severity = Severity.WARNING
            checks.append(
                CheckSpec(
                    name=f"[{b.table}.{c}] null rate within budget",
                    kind=CheckKind.NULL_RATE,
                    table=ref,
                    column=c,
                    severity=severity,
                    threshold=float(null_rate) if isinstance(null_rate, (int, float)) else 0.05,
                    rationale=(
                        f"observed {null_rate:.2%} nulls at plan time"
                        if isinstance(null_rate, (int, float))
                        else "generic 5% completeness budget"
                    ),
                    tags=["completeness"],
                )
            )

        # --- 5. value validity ------------------------------------------ #
        for c in cols:
            meta = col_prof.get(c, {})
            dtype = str(meta.get("data_type", "")).lower()
            if _EMAIL_RE.search(c):
                checks.append(
                    CheckSpec(
                        name=f"[{b.table}.{c}] looks like an email",
                        kind=CheckKind.PATTERN,
                        table=ref,
                        column=c,
                        severity=Severity.WARNING,
                        regex=r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$",
                        rationale="Identifier-format drift breaks downstream sends",
                        tags=["validity"],
                    )
                )
            elif _PCT_RE.search(c):
                checks.append(
                    CheckSpec(
                        name=f"[{b.table}.{c}] percentage within [0,1]",
                        kind=CheckKind.RANGE,
                        table=ref,
                        column=c,
                        severity=Severity.WARNING,
                        min_value=0,
                        max_value=1,
                        rationale="Rate columns must stay normalised; >1 signals a divide bug",
                        tags=["validity"],
                    )
                )
            elif _AMOUNT_RE.search(c) and not _KEY_RE.search(c):
                checks.append(
                    CheckSpec(
                        name=f"[{b.table}.{c}] amount is non-negative",
                        kind=CheckKind.RANGE,
                        table=ref,
                        column=c,
                        severity=Severity.INFO,
                        min_value=0,
                        max_value=None,
                        rationale="Negative monetary values usually indicate reversed signs in the ETL",
                        tags=["validity"],
                    )
                )
            elif _CURRENCY_RE.search(c):
                checks.append(
                    CheckSpec(
                        name=f"[{b.table}.{c}] is a 3-letter currency code",
                        kind=CheckKind.PATTERN,
                        table=ref,
                        column=c,
                        severity=Severity.WARNING,
                        regex=r"^[A-Z]{3}$",
                        rationale="ISO-4217 code expected",
                        tags=["validity"],
                    )
                )
            elif _STATUS_RE.search(c) and meta.get("distinct_values") and meta.get("distinct_values", 0) <= 20:
                checks.append(
                    CheckSpec(
                        name=f"[{b.table}.{c}] cardinality is stable",
                        kind=CheckKind.DISTRIBUTION,
                        table=ref,
                        column=c,
                        severity=Severity.INFO,
                        rationale=(
                            f"{meta['distinct_values']} distinct values observed at plan time; "
                            "watch for category explosion"
                        ),
                        tags=["drift"],
                    )
                )

        # --- 6. freshness on timestamp columns --------------------------- #
        for c in cols:
            meta = col_prof.get(c, {})
            dtype = str(meta.get("data_type", "")).lower()
            if any(k in dtype for k in ("timestamp", "date", "_dt", "time")):
                checks.append(
                    CheckSpec(
                        name=f"[{b.table}.{c}] data is fresh (<24h)",
                        kind=CheckKind.FRESHNESS,
                        table=ref,
                        column=c,
                        severity=Severity.CRITICAL if b.is_target else Severity.INFO,
                        rationale="Stale timestamps signal a broken upstream load",
                        params={"sla_hours": 24},
                        tags=["freshness"],
                    )
                )

    # --- 7. row-count parity against the driving table ------------------ #
    # Parity is only meaningful against the table whose grain the target
    # inherits. Comparing a target to a joined lookup table produces confident
    # nonsense, so we pair strictly via `analysis.grain` and skip anything
    # whose driver we could not identify.
    targets = [b for b in bindings if b.is_target]
    by_table = {b.table: b for b in bindings if not b.is_target}
    grain = analysis.grain if analysis else {}
    for t in targets:
        driver = grain.get(t.table)
        if not driver:
            continue
        s = by_table.get(_bare(driver))
        if s is None or not s.row_count or s.row_count <= 0 or t.row_count is None:
            continue
        ratio = abs(t.row_count - s.row_count) / s.row_count
        tolerance = 0.05 if ratio < 0.2 else 0.35
        checks.append(
            CheckSpec(
                name=f"[{t.table} vs {s.table}] row count within {tolerance:.0%}",
                kind=CheckKind.ROW_COUNT,
                table=_table_ref(t),
                severity=Severity.CRITICAL,
                tolerance_ratio=tolerance,
                rationale=(
                    f"{s.table} drives {t.table}: {s.row_count:,} rows vs {t.row_count:,} "
                    f"({ratio:.1%} drift at plan time)"
                ),
                params={"expected_row_count": s.row_count},
                tags=["parity"],
            )
        )

    # --- 8. file -> warehouse contract ----------------------------------- #
    # A data file the user handed us (a dbt seed, a CSV drop) promises a set of
    # columns. If that name also resolves to a live table, a column promised by
    # the file but absent from the table is real drift: the pipeline can pass
    # today and break the next time the file is re-uploaded.
    file_profiles = {p.table: p for p in (analysis.data_profiles if analysis else [])}
    for b in bindings:
        file_prof = file_profiles.get(b.table) or file_profiles.get(f"seed_{b.table}")
        live = {c.lower() for c in _columns(b)}
        if file_prof is None or not live:
            continue
        promised = [c for c in file_prof.column_names() if c.lower() not in live]
        if not promised:
            continue
        checks.append(
            CheckSpec(
                name=f"[{b.table}] columns promised by {file_prof.path} exist in the database",
                kind=CheckKind.SCHEMA,
                table=_table_ref(b),
                severity=Severity.WARNING,
                params={"expected_columns": sorted(file_prof.column_names())},
                rationale=(
                    f"{len(promised)} column(s) in {file_prof.path} "
                    f"({', '.join(promised[:5])}) are not in the live table — "
                    "the file and the warehouse disagree"
                ),
                tags=["contract", "schema"],
            )
        )

    # De-dup identical probes. Tags are part of the key on purpose: the baseline
    # "table is queryable" row-count check and the source->target parity check
    # probe the same table but answer different questions.
    seen: set[tuple] = set()
    unique: list[CheckSpec] = []
    for c in checks:
        key = (c.table.source_id, c.table.fqn, str(c.kind), c.column, tuple(sorted(c.tags)))
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)

    return unique[: settings.run_max_checks]


_EQ_RE = re.compile(r"([\w.\"`]+)\s*=\s*([\w.\"`]+)")


def _join_key(analysis: EtlAnalysis | None, target: str, source: str) -> tuple[str | None, str | None]:
    """Recover the join columns from the ETL: `(child_col, parent_col)`.

    Handles `a.x = b.y`, `a.x = b.x` and `USING (x)`; returns (None, None) when
    the join key cannot be recovered, so we never emit a broken check.
    """
    if analysis is None:
        return None, None
    for j in analysis.joins or []:
        rt = _bare(j.get("right_table") or "")
        if rt != source and _bare(target) not in {rt}:
            continue

        for used in j.get("using") or []:
            return used, used

        on = j.get("on") or ""
        # `o.customer_id = c.customer_id` -> aliases o (child) and c (parent)
        pairs = _EQ_RE.findall(on)
        if len(pairs) != 1:
            return None, None
        left, right = pairs[0]
        left_alias, left_col = _split_qual(left)
        right_alias, right_col = _split_qual(right)
        if not left_col or not right_col:
            return None, None

        # the side that is NOT the joined table belongs to the target
        if right_alias and _bare(right_alias) == rt:
            return left_col, right_col
        if left_alias and _bare(left_alias) == rt:
            return right_col, left_col
        return left_col, right_col
    return None, None


def _split_qual(ref: str) -> tuple[str | None, str]:
    ref = ref.strip('`"[]')
    if "." in ref:
        alias, col = ref.rsplit(".", 1)
        return alias, col
    return None, ref


def summarize_analysis(analysis: EtlAnalysis | None) -> str:
    if analysis is None:
        return "No ETL script supplied. Planning tests directly from source schemas."
    parts = [
        f"language: {analysis.language}",
        f"source tables: {', '.join(analysis.sources) or 'n/a'}",
        f"target tables: {', '.join(analysis.targets) or 'n/a'}",
    ]
    if analysis.transformations:
        parts.append("transformations:")
        parts.extend(f"  - [{t.kind}] {t.description}" for t in analysis.transformations[:25])
    if analysis.lineage:
        parts.append("lineage: " + ", ".join(f"{e.source} -> {e.target}" for e in analysis.lineage[:15]))
    if analysis.risks:
        parts.append("risks: " + "; ".join(analysis.risks))
    return "\n".join(parts)


__all__ = ["plan_checks", "bind_sources", "summarize_analysis", "SourceBinding"]


def log_plan(checks: list[CheckSpec]) -> None:
    log.info("deterministic plan built", checks=len(checks))