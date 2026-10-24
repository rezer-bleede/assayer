"""Combines the deterministic plan with optional LLM-generated checks."""

from __future__ import annotations

import asyncio
import json
import math
import re
from typing import Any, cast
from urllib.parse import urljoin, urlparse

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.agent.planner import SourceBinding, plan_checks, summarize_analysis
from app.agent.prompts import plan_prompt
from app.core.config import settings
from app.core.llm import ThoughtCallback, ainvoke, llm_available
from app.core.logging import log

# Single enforcement point with the runner: the same tuple guards compile_check,
# so a column-less check cannot be produced where it would break, either.
from app.runner.compiler import NEEDS_COLUMN
from app.schemas import (
    CheckKind,
    CheckSpec,
    ConnectionSpec,
    EtlAnalysis,
    Severity,
    SourceType,
    TableRef,
    TestPlan,
    UiStep,
)

_ALLOWED_KINDS = {k.value for k in CheckKind}
_ALLOWED_SEVERITIES = {s.value for s in Severity}
_UI_KINDS = {"page_load", "element_present", "element_text", "console_clean", "network_clean", "ui_parity", "flow", "visual_diff"}


def _requests_rebaseline(user_intent: str | None) -> bool:
    text = user_intent or ""
    action = re.search(r"\b(rebaseline|reset|replace|update|recapture)\b", text, re.I)
    visual = re.search(r"\b(visual|screenshot|image)\b", text, re.I)
    negative = re.search(
        r"\b(don't|do not|never|not|avoid|without)\b.{0,60}\b(rebaseline|reset|replace|update|recapture)\b",
        text,
        re.I,
    )
    return bool(action and visual and not negative)


def _coerce_ui(
    raw: dict,
    connections: dict[str, ConnectionSpec],
    bindings: list[SourceBinding],
    user_intent: str | None = None,
    route_info: list[dict[str, Any]] | None = None,
) -> CheckSpec | None:
    try:
        kind = str(raw.get("kind", "")).lower()
        cid = str(raw.get("source_id", ""))
        spec = connections.get(cid)
        if kind not in _UI_KINDS or spec is None or spec.type != SourceType.WEB:
            return None
        route = str(raw.get("url") or "/")
        parsed = urlparse(route)
        base = urlparse(spec.url or "")
        if route.startswith("//") or "\\" in route:
            return None
        if parsed.scheme and (parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)) != (
            base.scheme, base.hostname, base.port or (443 if base.scheme == "https" else 80)
        ):
            return None
        if not parsed.scheme and not route.startswith("/"):
            route = f"/{route}"
            parsed = urlparse(route)
        known = {str(item.get("path")) for item in (route_info or []) if item.get("path")}
        if known and parsed.path not in known:
            return None
        selector = str(raw.get("selector") or "")[:300] or None
        expected = str(raw["expected"])[:500] if raw.get("expected") is not None else None
        params = cast(dict, raw.get("params")) if isinstance(raw.get("params"), dict) else {}
        steps = cast(list[dict], raw.get("steps")) if isinstance(raw.get("steps"), list) else []
        if len(steps) > settings.ui_max_steps or (kind in {"element_present", "element_text", "ui_parity"} and not selector):
            return None
        if kind == "element_text" and not expected:
            return None
        if kind == "flow" and not steps:
            return None
        if kind == "ui_parity":
            query = str(params.get("query") or "")
            if str(params.get("source_id") or "") not in {b.source_id for b in bindings}:
                return None
            from app.runner.compiler import assert_readonly
            assert_readonly(query)
        severity = str(raw.get("severity", "warning")).lower()
        validated_steps = [UiStep.model_validate(step) for step in steps]
        if any(len(step.selector or "") > 300 or len(step.value or "") > 500 for step in validated_steps):
            return None
        selectors: set[str] = set()
        for item in route_info or []:
            if item.get("path") == parsed.path:
                selectors.update(item.get("selectors") or [])
        if selector and selectors and selector not in selectors:
            return None
        active_path = parsed.path
        for step in validated_steps:
            if step.action == "goto" and step.value:
                resolved_step = urljoin((spec.url or "").rstrip("/") + "/", step.value)
                resolved = urlparse(resolved_step)
                active_path = resolved.path
                if (resolved.scheme, resolved.hostname, resolved.port or (443 if resolved.scheme == "https" else 80)) != (
                    base.scheme, base.hostname, base.port or (443 if base.scheme == "https" else 80)
                ):
                    return None
                if known and active_path not in known:
                    return None
            step_selectors: set[str] = set()
            for item in route_info or []:
                if item.get("path") == active_path:
                    step_selectors.update(item.get("selectors") or [])
            if step.selector and step_selectors and step.selector not in step_selectors:
                return None
        safe_params = {k: v for k, v in params.items() if k in {"query", "source_id", "tolerance", "ignore_patterns", "title_pattern", "viewport", "settle_ms"}}
        if "ignore_patterns" in safe_params and (
            not isinstance(safe_params["ignore_patterns"], list)
            or len(safe_params["ignore_patterns"]) > 30
            or any(len(str(p)) > 200 for p in safe_params["ignore_patterns"][:30])
        ):
            return None
        title_pattern = safe_params.get("title_pattern")
        if title_pattern is not None:
            if not isinstance(title_pattern, str) or len(title_pattern) > 500:
                return None
            re.compile(title_pattern)
        if kind == "page_load" and expected:
            re.compile(expected)
        if "viewport" in safe_params:
            viewport = safe_params["viewport"]
            if not isinstance(viewport, dict):
                return None
            width, height = viewport.get("width", 1280), viewport.get("height", 720)
            if not isinstance(width, int) or not isinstance(height, int) or not (
                320 <= width <= 3840 and 240 <= height <= 2160
            ):
                return None
        if "settle_ms" in safe_params and (
            not isinstance(safe_params["settle_ms"], int) or not 0 <= safe_params["settle_ms"] <= 2000
        ):
            return None
        rebaseline_requested = _requests_rebaseline(user_intent)
        if rebaseline_requested and kind == "visual_diff":
            safe_params["rebaseline"] = True
        if "tolerance" in safe_params and (
            not isinstance(safe_params["tolerance"], (int, float))
            or not math.isfinite(safe_params["tolerance"])
            or safe_params["tolerance"] < 0
        ):
            return None
        return CheckSpec(
            name=str(raw.get("name") or f"{spec.name} {kind}")[:160], kind=CheckKind(kind),
            source_id=cid, url=route, selector=selector, expected=expected,
            severity=Severity(severity) if severity in _ALLOWED_SEVERITIES else Severity.WARNING,
            steps=validated_steps,
            params=safe_params,
            rationale=str(raw.get("rationale") or "")[:400], tags=["llm", "ui"],
        )
    except Exception:
        return None


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
                log.debug(
                    "dropping llm check: unknown parent_column", parent=parent, column=parent_col
                )
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
            params={
                k: v
                for k, v in params.items()
                if k
                in {
                    "parent_table",
                    "parent_column",
                    "parent_schema",
                    "sla_hours",
                    "expected_row_count",
                    "keys",
                    "expected_columns",
                }
            },
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
async def _ask_llm(prompt: str, on_thought: ThoughtCallback | None = None) -> str:
    # A thinking model reasons before it writes the JSON; the cap keeps that
    # monologue from running forever on a runner that never closes the stream.
    return await ainvoke(prompt, max_tokens=2000, on_thought=on_thought)


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
    on_thought: ThoughtCallback | None = None,
    project_context: str | None = None,
    connections: dict[str, ConnectionSpec] | None = None,
) -> TestPlan:
    base = plan_checks(analysis, bindings, profile=profiles)
    ui_checks: list[CheckSpec] = []
    ui_routes: dict[str, list[dict]] = {}
    rebaseline_requested = _requests_rebaseline(user_intent)
    for cid, spec in (connections or {}).items():
        if spec.type != SourceType.WEB or not settings.ui_enabled:
            continue
        base_path = urlparse(spec.url or "").path.rstrip("/")
        routes = [f"{base_path}/" if base_path else "/"]
        route_info = [{"path": routes[0], "title": "", "selectors": []}]
        try:
            from app.connectors.browser import BrowserClient

            def discover(target: ConnectionSpec = spec) -> list[dict]:
                with BrowserClient(target) as browser:
                    return browser.discover_routes()

            route_info = await asyncio.to_thread(discover) or route_info
            routes = [str(route["path"]) for route in route_info if route.get("path")] or routes
        except Exception as exc:  # noqa: BLE001 - discovery enriches the root smoke plan
            log.info("UI route discovery unavailable; using root route", target=spec.name, error=str(exc))
        ui_routes[cid] = route_info
        for route in routes[: settings.ui_max_pages]:
            for kind, name, severity, selector in (
                (CheckKind.PAGE_LOAD, "page loads", Severity.BLOCKER if route == routes[0] else Severity.CRITICAL, None),
                (CheckKind.CONSOLE_CLEAN, "has no console errors", Severity.WARNING, None),
                (CheckKind.NETWORK_CLEAN, "has no failed requests", Severity.WARNING, None),
                (CheckKind.ELEMENT_PRESENT, "has a main content region", Severity.WARNING, "main"),
            ):
                ui_checks.append(CheckSpec(
                    name=f"[{spec.name} {route}] {name}", kind=kind, source_id=cid, url=route,
                    selector=selector, severity=severity,
                    rationale="Deterministic frontend smoke check", tags=["ui", "baseline"],
                ))
            ui_checks.append(CheckSpec(
                name=f"[{spec.name} {route}] visual layout baseline", kind=CheckKind.VISUAL_DIFF,
                source_id=cid, url=route, severity=Severity.INFO,
                params={"rebaseline": True} if rebaseline_requested else {},
                rationale="Records a screenshot baseline and detects future visual drift",
                tags=["ui", "visual"],
            ))
            ui_checks.append(CheckSpec(
                name=f"[{spec.name} {route}] navigation flow", kind=CheckKind.FLOW,
                source_id=cid, url=route, severity=Severity.INFO,
                steps=[UiStep(action="goto", value=route), UiStep(action="assert_present", selector="body")],
                rationale="Verifies route navigation and page rendering",
                tags=["ui", "flow", "baseline"],
            ))

    if ui_checks:
        # Keep the plan within the same cap enforced by the runner. Reserve
        # capacity for the UI suite so it cannot be silently sliced off behind
        # a large data-only baseline.
        ui_checks = ui_checks[: settings.run_max_checks]
        base = base[: max(0, settings.run_max_checks - len(ui_checks))] + ui_checks

    has_ui_target = settings.ui_enabled and any(
        spec.type == SourceType.WEB for spec in (connections or {}).values()
    )
    if not (use_llm and llm_available() and (bindings or has_ui_target)):
        plan = TestPlan(
            summary=(
                f"{len(base)} checks derived from "
                + (
                    f"the {analysis.language} ETL parse and live source schemas"
                    if analysis
                    else "live source schemas"
                )
            ),
            checks=base,
        )
        return plan

    src_block: list[dict[str, Any]] = [
        {"id": b.source_id, "name": b.name, "type": "", "schema": b.schema, "table": b.table}
        for b in bindings
    ]
    src_block.extend(
        {"id": cid, "name": spec.name, "type": "web", "schema": None, "table": spec.url or "", "routes": ui_routes.get(cid, [])}
        for cid, spec in (connections or {}).items()
        if settings.ui_enabled and spec.type == SourceType.WEB
    )
    try:
        raw = await _ask_llm(
            plan_prompt(summarize_analysis(analysis), src_block, user_intent, project_context),
            on_thought=on_thought,
        )
        parsed = _extract_json(raw)
        proposed = parsed.get("checks", []) if isinstance(parsed.get("checks", []), list) else []
        llm_checks = [
            c for c in (
                _coerce_ui(r, connections or {}, bindings, user_intent, ui_routes.get(str(r.get("source_id", "")), []))
                if str(r.get("kind", "")).lower() in _UI_KINDS else _coerce(r, bindings)
                for r in proposed
            ) if c
        ]
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
    return TestPlan(
        summary=f"{summary} ({len(base)} baseline + {len(llm_checks)} agent checks)", checks=merged
    )


def _merge(base: list[CheckSpec], extra: list[CheckSpec]) -> list[CheckSpec]:
    def key(c: CheckSpec) -> tuple:
        if c.table:
            return (c.table.source_id, c.table.fqn, str(c.kind), c.column)
        return (
            c.source_id,
            c.url or "",
            str(c.kind),
            c.selector,
            c.expected,
            tuple((step.action, step.selector, step.value) for step in c.steps),
            json.dumps(c.params, sort_keys=True, default=str),
        )

    seen = {key(c) for c in base}
    merged = list(base)
    for c in extra:
        ckey = key(c)
        if ckey in seen:
            continue
        seen.add(ckey)
        merged.append(c)
    sev_rank = {"blocker": 0, "critical": 1, "warning": 2, "info": 3}
    merged.sort(key=lambda c: (sev_rank.get(str(c.severity), 9), c.table.fqn if c.table else (c.url or ""), str(c.kind)))
    return merged
