"""Combines the deterministic plan with optional LLM-generated checks."""

from __future__ import annotations

import json

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.agent.planner import SourceBinding, plan_checks, summarize_analysis
from app.agent.prompts import plan_prompt
from app.core.llm import ainvoke, llm_available
from app.core.logging import log
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


def _coerce(raw: dict, bindings: list[SourceBinding]) -> CheckSpec | None:
    """Turn one LLM check dict into a validated CheckSpec, or drop it."""
    try:
        kind = str(raw.get("kind", "")).lower()
        if kind not in _ALLOWED_KINDS:
            return None
        severity = str(raw.get("severity", "critical")).lower()
        if severity not in _ALLOWED_SEVERITIES:
            severity = "critical"

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

        params = raw.get("params") or {}
        if not isinstance(params, dict):
            params = {}

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
        llm_checks = [c for c in (_coerce(r, bindings) for r in parsed.get("checks", [])) if c]
    except Exception as exc:  # noqa: BLE001 - LLM is an enhancement, never a hard dep
        log.warning("llm planning failed, using deterministic plan", error=str(exc))
        return TestPlan(
            summary=f"{len(base)} deterministic checks (LLM unavailable: {type(exc).__name__})",
            checks=base,
        )

    merged = _merge(base, llm_checks)
    summary = str(parsed.get("summary") or "").strip() or f"{len(merged)} checks"
    log.info("plan built", deterministic=len(base), llm=len(llm_checks), merged=len(merged))
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