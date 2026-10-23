"""Deterministic OpenUI Lang programs for each conversation surface.

The LLM is asked to author the same shapes from the generated prompt; these
serializers are the reliability floor. They are intentionally boring: valid
programs that render the exact server state, with no invented data.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

from app.schemas import (
    ConnectionSpec,
    EtlAnalysis,
    Question,
    QuestionKind,
    RunResult,
    SourceRequirement,
    TestPlan,
)


def _q(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _s(value: Any) -> str:
    return "null" if value is None else _q(value)


def _arr(items: list[str]) -> str:
    return "[" + ", ".join(items) + "]"


def _obj(items: dict[str, Any]) -> str:
    parts = []
    for key, value in items.items():
        if value is None:
            continue
        if isinstance(value, list):
            parts.append(f"{key}: {_arr([_value(v) for v in value])}")
        elif isinstance(value, dict):
            parts.append(f"{key}: {_obj(value)}")
        else:
            parts.append(f"{key}: {_value(value)}")
    return "{" + ", ".join(parts) + "}"


def _value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return _q(value)
    if isinstance(value, list):
        return _arr([_value(v) for v in value])
    if isinstance(value, dict):
        return _obj(value)
    return _q(str(value))


def _card(children: list[str]) -> str:
    return f"root = Card({_arr(children)})"


def notice_component(
    body: str,
    *,
    title: str | None = None,
    tone: str = "neutral",
    bullets: list[str] | None = None,
    actions: list[str] | None = None,
) -> str:
    args = [_s(title), _q(body), _q(tone)]
    if bullets:
        args.append(_arr([_q(b) for b in bullets]))
    if actions:
        if not bullets:
            args.append("null")
        args.append(_arr([_q(a) for a in actions]))
    return f"Notice({', '.join(args)})"


def notice_program(
    body: str,
    *,
    title: str | None = None,
    tone: str = "neutral",
    bullets: list[str] | None = None,
    actions: list[str] | None = None,
) -> str:
    return _card([notice_component(body, title=title, tone=tone, bullets=bullets, actions=actions)])


def welcome_program(title: str, body: str, examples: list[str] | None = None) -> str:
    args = [_q(title), _q(body)]
    if examples:
        args.append(_arr([_q(e) for e in examples]))
    return _card([f"Welcome({', '.join(args)})"])


def _option(
    question: Question,
    kind: QuestionKind,
    *,
    label: str,
    help: str | None = None,
    placeholder: str | None = None,
    requirement: SourceRequirement | None = None,
    analysis: EtlAnalysis | None = None,
    preferred: bool = True,
) -> str:
    fields: dict[str, Any] = {
        "kind": str(kind),
        "label": label,
        "help": help,
        "placeholder": placeholder,
        "requirementKey": question.requirement_key,
        "preferred": preferred,
    }
    if requirement:
        fields["tables"] = requirement.tables
        if analysis:
            fields["targetTables"] = analysis.targets
    return _obj(fields)


def question_program(question: Question, analysis: EtlAnalysis | None = None) -> str:
    requirement = None
    if analysis and question.requirement_key:
        requirement = next(
            (r for r in analysis.required_sources if r.key == question.requirement_key), None
        )

    options: list[str] = []
    if question.stage.value == "need_etl" or not analysis:
        for opt in question.options:
            if opt.kind == QuestionKind.GIT_URL:
                options.append(
                    _option(
                        question,
                        opt.kind,
                        label=opt.label,
                        help=opt.help,
                        placeholder=opt.placeholder,
                        preferred=opt.preferred,
                    )
                )
            elif opt.kind == QuestionKind.FILE_UPLOAD:
                options.append(
                    _option(
                        question,
                        opt.kind,
                        label=opt.label,
                        help=opt.help,
                        preferred=opt.preferred,
                    )
                )
    for opt in question.options:
        if opt.kind == QuestionKind.CONNECTION:
            options.append(
                _option(
                    question,
                    opt.kind,
                    label=opt.label,
                    help=opt.help,
                    placeholder=opt.placeholder,
                    requirement=requirement,
                    analysis=analysis,
                    preferred=opt.preferred,
                )
            )

    if not options:
        actions = [o.label for o in question.options if o.kind == QuestionKind.PASTE]
        return notice_component(
            question.prompt,
            title=question.title,
            bullets=[f"{o.label}: {o.help}" for o in question.options if o.help],
            actions=actions or None,
        )

    args = [
        _q(question.title),
        _q(question.prompt),
        _arr(options),
        "true" if question.skip_allowed else "false",
    ]
    return f"IntakeCard({', '.join(args)})"


def etl_program(analysis: EtlAnalysis) -> str:
    profiles = []
    for p in analysis.data_profiles[:8]:
        profiles.append(
            {
                "path": p.path,
                "table": p.table,
                "format": p.format,
                "rowCount": p.row_count,
                "columns": [{"name": c.name, "dataType": c.data_type} for c in p.columns[:16]],
            }
        )
    args = [
        _s(analysis.label),
        _q(str(analysis.origin)),
        _q(str(analysis.language)),
        _arr([_q(f) for f in analysis.files[:40]]),
        _arr([_q(s) for s in analysis.sources[:30]]),
        _arr([_q(t) for t in analysis.targets[:30]]),
        _arr([_q(r) for r in analysis.risks[:12]]),
        _arr([_obj(p) for p in profiles]),
    ]
    return f"EtlCard({', '.join(args)})"


def plan_program(plan: TestPlan, missing: list[str] | None = None) -> str:
    checks = []
    for c in plan.checks[:80]:
        checks.append(
            _obj(
                {
                    "id": c.id,
                    "name": c.name,
                    "kind": str(c.kind),
                    "table": c.table.fqn,
                    "column": c.column,
                    "severity": str(c.severity),
                    "rationale": c.rationale,
                }
            )
        )
    args = [
        _q(plan.run_id or plan.id),
        _s(plan.summary),
        _arr(checks),
        _arr([_q(m) for m in (missing or [])]),
        _arr([_q("show me the failed checks"), _q("regenerate the plan")]),
    ]
    return f"PlanCard({', '.join(args)})"


def _observed(value: float | str | None) -> float | str | None:
    return value


def report_program(run: RunResult) -> str:
    checks = []
    for r in run.results[:120]:
        checks.append(
            _obj(
                {
                    "checkId": r.check_id,
                    "name": r.name,
                    "kind": str(r.kind),
                    "table": r.table.fqn,
                    "column": r.column,
                    "severity": str(r.severity),
                    "status": str(r.status),
                    "observed": _observed(r.observed),
                    "expected": _observed(r.expected),
                    "failedRows": r.failed_rows,
                    "message": r.message,
                }
            )
        )
    score = run.score
    args = [
        _q(run.id),
        str(float(score.overall)),
        str(int(run.duration_ms)) if run.duration_ms is not None else "null",
        _s((run.narrative or "").split("\n\n")[0][:600] or None),
        _obj(
            {
                "passed": score.passed,
                "failed": score.failed,
                "errored": score.errored,
                "skipped": score.skipped,
            }
        ),
        _arr(checks),
        _arr([_q("show me only the failed checks"), _q("explain the failures")]),
    ]
    return f"AuditReport({', '.join(args)})"


def connection_program(spec: ConnectionSpec, *, ok: bool = True, message: str | None = None) -> str:
    args = [
        _q(spec.name),
        _q(str(spec.type)),
        "true" if ok else "false",
        _s(message),
        _arr([]),
        _arr([]),
        "null",
    ]
    return _card([f"ConnectionCard({', '.join(args)})"])


def repair_component(spec: ConnectionSpec | None, note: str | None = None) -> str:
    args = [
        _s(note),
        _s(spec.host if spec else None),
        str(spec.port) if spec and spec.port else "null",
        _s(spec.database if spec else None),
    ]
    return f"RepairCard({', '.join(args)})"


def repair_program(spec: ConnectionSpec | None, note: str | None = None) -> str:
    return _card([repair_component(spec, note)])


def program_for_state(
    *,
    ctx: dict[str, Any],
    question: Question | None,
    plan: TestPlan | None,
    run: RunResult | None,
    analysis: EtlAnalysis | None,
    notes: list[str] | None = None,
) -> str:
    children: list[str] = []
    if run is not None:
        children.append(report_program(run))
    elif plan is not None:
        children.append(plan_program(plan, ctx.get("missing_tables")))
    elif ctx.get("pending_connection"):
        pending = ctx["pending_connection"]
        spec = None
        with contextlib.suppress(Exception):
            spec = ConnectionSpec.model_validate(pending.get("spec") or {})
        children.append(repair_component(spec, pending.get("note")))
        if question is not None:
            children.append(question_program(question, analysis))
    elif question is not None:
        if analysis is not None:
            children.append(etl_program(analysis))
        children.append(question_program(question, analysis))
    elif analysis is not None:
        children.append(etl_program(analysis))
    if not children:
        body = "\n\n".join(notes or []) or (
            "Tell me which codebase to audit, or upload your ETL project, and I'll work "
            "out what needs testing."
        )
        return notice_program(body)
    return _card(children)


def state_snapshot(
    *,
    ctx: dict[str, Any],
    question: Question | None,
    plan: TestPlan | None,
    run: RunResult | None,
    analysis: EtlAnalysis | None,
    specs: dict[str, ConnectionSpec],
    notes: list[str] | None = None,
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "stage": ctx.get("stage"),
        "notes": (notes or [])[-3:],
        "missing_tables": ctx.get("missing_tables", []),
        "pending_connection": bool(ctx.get("pending_connection")),
        "connections": [s.redacted() for s in specs.values()],
    }
    if analysis is not None:
        state["etl"] = {
            "label": analysis.label,
            "origin": str(analysis.origin),
            "language": str(analysis.language),
            "files": analysis.files[:20],
            "sources": analysis.sources[:20],
            "targets": analysis.targets[:20],
            "risks": analysis.risks[:10],
            "data_profiles": [
                {
                    "path": p.path,
                    "table": p.table,
                    "format": p.format,
                    "row_count": p.row_count,
                    "columns": [{"name": c.name, "data_type": c.data_type} for c in p.columns[:10]],
                }
                for p in analysis.data_profiles[:5]
            ],
        }
    if question is not None:
        state["question"] = question.model_dump(mode="json")
    if plan is not None:
        state["plan"] = {
            "run_id": plan.run_id or plan.id,
            "summary": plan.summary,
            "checks": [
                {
                    "id": c.id,
                    "name": c.name,
                    "kind": str(c.kind),
                    "table": c.table.fqn,
                    "column": c.column,
                    "severity": str(c.severity),
                    "rationale": c.rationale,
                }
                for c in plan.checks[:60]
            ],
        }
    if run is not None:
        state["run"] = {
            "id": run.id,
            "score": run.score.model_dump(mode="json"),
            "duration_ms": run.duration_ms,
            "narrative": (run.narrative or "")[:1200],
            "results": [
                {
                    "check_id": c.check_id,
                    "name": c.name,
                    "kind": str(c.kind),
                    "table": c.table.fqn,
                    "column": c.column,
                    "severity": str(c.severity),
                    "status": str(c.status),
                    "observed": c.observed,
                    "expected": c.expected,
                    "failed_rows": c.failed_rows,
                    "message": c.message,
                }
                for c in run.results[:80]
            ],
        }
    return state
