"""Combines the deterministic plan with optional LLM-generated checks."""

from __future__ import annotations

import json

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.agent.planner import SourceBinding, plan_checks, summarize_analysis
from app.agent.prompts import plan_prompt
from app.core.llm import ainvoke, llm_available
from app.core.logging import log

# Single enforcement point with the runner: the same tuple guards compile_check,
# so a column-less check cannot be produced where it would break, either.
from app.runner.compiler import NEEDS_COLUMN
from app.schemas import (
    CheckKind,
    CheckSpec,
    EtlAnalysis,
    Severity,
    TableRef,
    TestPlan,
)

_ALLOWED_KINDS = {k.value for k in CheckKind}
_ALLOWED_SEVERITIES = {s.value for s in Severity}


def _known_columns(binding: SourceBinding) -> set[str] | None:
    """Lower-cased column names the profiler saw, or None when unprofiled."""
    lowered = {str(c).lower() for c in (binding.columns or []) if c}
    return lowered or None


def _coerce(raw: dict, bindings: list[SourceBinding]) -> CheckSpec | None:
    """Turn one LLM check dict into a validated CheckSpec, or drop it."""
    try:
        kind = str(raw.get("kind", "")).lower()
        if kind not in _ALLOWED_KINDS:
            return None
        severity = str(raw.get("severity", "critical")).lower()
        if severity not in _ALLOWED_SEVERITIES:
            severity = "critical"

        # Column-level kinds must name a column: a missing one turns into
        # literal `None` in the compiled SQL and fails late. Table-level
        # aggregates (row_count/volume) are the only checks that may stay
        # nameless.
        if kind in NEEDS_COLUMN and not str(raw.get("column") or "").strip():
            log.debug("dropping llm check: kind needs a column", kind=kind)
            return None

        table_name = str(raw.get("table") or "").strip()
        source_id = str(raw.get("source_id") or "").strip()
        binding = next(
            (b for b in bindings if b.source_id == source_id and b.table == table_name.lower()),
            None,
        )
        if binding is None and len(bindings) == 1:
            binding = bindings[0]
        if binding is None:
            return None

        # A check that names a column the profiler never saw cannot run — the
        # current database throws UndefinedColumn and the check reports as an
        # error. Small models invent plausible column names; drop those here.
        column = raw.get("column")
        known = _known_columns(binding)
        if column and known and str(column).lower() not in known:
            log.debug("dropping llm check: unknown column", table=binding.table, column=column)
            return None

        params = raw.get("params") or {}
        if not isinstance(params, dict):
            params = {}

        # The parent side of a referential check is validated the same way:
        # against the other bindings' live names, not against the model's
        # imagination of the warehouse.
        if kind == "referential":
            parent = str(params.get("parent_table") or "").strip().split(".")[-1].lower()
            parent_binding = next((b for b in bindings if b.table.lower() == parent), None)
            if parent_binding is None:
                log.debug("dropping llm check: unknown parent_table", parent=parent)
                return None
            parent_col = str(params.get("parent_column") or "").strip()
            parent_known = _known_columns(parent_binding)
            if parent_col and parent_known and parent_col.lower() not in parent_known:
                log.debug("dropping llm check: unknown parent_column", parent=parent, column=parent_col)
                return None

        return CheckSpec(
            name=str(raw.get("name") or f"{binding.table} {kind}")[:160],
            kind=CheckKind(kind),
            table=TableRef(
                source_id=binding.source_id,
                schema=raw.get("schema") or binding.schema,
                name=binding.table,
            ),
            column=raw.get("column") or None,
            severity=Severity(severity),
            threshold=_num(raw.get("threshold")),
            min_value=raw.get("min_value"),
            max_value=raw.get("max_value"),
            tolerance_ratio=_num(raw.get("tolerance_ratio")) or 0.0,
            regex=raw.get("regex"),
            sql=raw.get("sql") if kind == "custom_sql" else None,
            params={k: v for k, v in params.items() if k in
                    {"parent_table", "parent_column", "parent_schema", "sla_hours",
                     "expected_row_count", "keys", "expected_columns"}},
            rationale=str(raw.get("rationale") or "")[:400],
            tags=["llm"],
        )
    except Exception as exc:  # noqa: BLE001 - a malformed check must not kill the plan
        log.debug("dropping malformed llm check", error=str(exc))
        return None


def _num(v: object) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


@retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=1, min=1, max=5),
    retry=retry_if_exception_type((RuntimeError, TimeoutError)),
    reraise=True,
)
async def _ask_llm(prompt: str) -> str:
    return await ainvoke(prompt)


def _extract_json(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```")[1]
        if t.lower().startswith("json"):
            t = t[4:]
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in LLM response")
    return json.loads(t[start : end + 1])


async def build_plan(
    analysis: EtlAnalysis | None,
    bindings: list[SourceBinding],
    user_intent: str | None = None,
    use_llm: bool = True,
    profiles: dict[str, dict] | None = None,
) -> TestPlan:
    base = plan_checks(analysis, bindings, profile=profiles)

    if not (use_llm and llm_available() and bindings):
        plan = TestPlan(
            summary=(
                f"{len(base)} checks derived from "
                + (f"the {analysis.language} ETL parse and live source schemas"
                   if analysis else "live source schemas")
            ),
            checks=base,
        )
        return plan

    src_block = [
        {"id": b.source_id, "name": b.name, "type": "", "schema": b.schema, "table": b.table}
        for b in bindings
    ]
    try:
        raw = await _ask_llm(
            plan_prompt(summarize_analysis(analysis), src_block, user_intent)
        )
        parsed = _extract_json(raw)
        proposed = parsed.get("checks", []) if isinstance(parsed.get("checks", []), list) else []
        llm_checks = [c for c in (_coerce(r, bindings) for r in proposed) if c]
    except Exception as exc:  # noqa: BLE001 - LLM is an enhancement, never a hard dep
        log.warning("llm planning failed, using deterministic plan", error=str(exc))
        return TestPlan(
            summary=f"{len(base)} deterministic checks (LLM unavailable: {type(exc).__name__})",
            checks=base,
        )

    merged = _merge(base, llm_checks)
    summary = str(parsed.get("summary") or "").strip() or f"{len(merged)} checks"
    log.info(
        "plan built",
        deterministic=len(base),
        suggested=len(proposed),
        accepted=len(llm_checks),
        merged=len(merged),
    )
    return TestPlan(summary=f"{summary} ({len(base)} baseline + {len(llm_checks)} agent checks)", checks=merged)


def _merge(base: list[CheckSpec], extra: list[CheckSpec]) -> list[CheckSpec]:
    seen = {(c.table.source_id, c.table.fqn, str(c.kind), c.column) for c in base}
    merged = list(base)
    for c in extra:
        key = (c.table.source_id, c.table.fqn, str(c.kind), c.column)
        if key in seen:
            continue
        seen.add(key)
        merged.append(c)
    sev_rank = {"blocker": 0, "critical": 1, "warning": 2, "info": 3}
    merged.sort(key=lambda c: (sev_rank.get(str(c.severity), 9), c.table.fqn, str(c.kind)))
    return merged