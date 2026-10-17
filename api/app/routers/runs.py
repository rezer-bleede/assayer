from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from app.agent.plan_builder import build_plan
from app.agent.planner import bind_sources, find_unresolved
from app.core.logging import log
from app.schemas import ConnectionSpec, RunResult, RunStatus, TestPlan
from app.services.orchestrator import introspect_all, profile_focus, run_audit
from app.store import repository as repo

router = APIRouter(prefix="/api/runs", tags=["runs"])


class PlanRequest(BaseModel):
    session_id: str | None = None
    connection_ids: list[str] = Field(default_factory=list)
    etl_id: str | None = None
    user_intent: str | None = None
    use_llm: bool = True


class PlanResponse(BaseModel):
    run_id: str
    plan: TestPlan
    missing_tables: list[str] = Field(default_factory=list)
    connections: list[dict[str, Any]]


@router.post("/plan", response_model=PlanResponse)
async def create_plan(req: PlanRequest) -> PlanResponse:
    """Profile sources + design the plan, then wait for human approval."""
    if not req.connection_ids:
        raise HTTPException(400, "connection_ids is required")

    connections: dict[str, ConnectionSpec] = {}
    for cid in req.connection_ids:
        spec = repo.get_connection(cid)
        if not spec:
            raise HTTPException(404, f"connection {cid} not found")
        connections[cid] = spec

    introspected = introspect_all(connections)
    analysis = repo.get_etl(req.etl_id) if req.etl_id else None
    bindings = bind_sources(analysis, connections, introspected) if analysis else []
    profiles = profile_focus(
        connections, introspected, [(b.schema, b.table) for b in bindings]
    )
    plan = await build_plan(
        analysis, bindings, req.user_intent, req.use_llm, profiles=profiles
    )
    missing = find_unresolved(analysis, bindings)
    if not plan.checks:
        raise HTTPException(
            409,
            "no checks could be generated: none of the ETL tables matched the connected "
            "sources. Bind explicitly on the connection spec, e.g. "
            'options={"table": "daily_revenue", "schema": "analytics", "role": "target"}.',
        )

    run_id = plan.id
    plan.run_id = run_id
    repo.save_plan(plan, req.session_id)
    repo.save_run(
        RunResult(id=run_id, status=RunStatus.AWAITING_APPROVAL, plan=plan),
        req.session_id,
    )
    log.info("plan created", run_id=run_id, checks=len(plan.checks))
    return PlanResponse(
        run_id=run_id,
        plan=plan,
        missing_tables=missing,
        connections=[c.redacted() for c in connections.values()],
    )


class ApproveRequest(BaseModel):
    session_id: str | None = None
    connection_ids: list[str] = Field(default_factory=list)
    checks: list[dict[str, Any]] | None = Field(
        default=None, description="Edited checks; omit to run the plan as-is"
    )
    approved: bool = True


@router.post("/{run_id}/approve")
async def approve(run_id: str, req: ApproveRequest) -> dict[str, Any]:
    plan = repo.get_plan(run_id)
    if not plan:
        raise HTTPException(404, "plan not found")
    if req.checks:
        from app.schemas import CheckSpec

        plan.checks = [CheckSpec.model_validate(c) for c in req.checks]

    connections: dict[str, ConnectionSpec] = {}
    for cid in req.connection_ids:
        spec = repo.get_connection(cid)
        if spec:
            connections[cid] = spec
    if not connections:
        raise HTTPException(400, "connection_ids is required to execute")

    result = await run_audit(run_id, plan, connections, session_id=req.session_id)
    return {"run": result.model_dump(mode="json")}


@router.get("")
def list_runs(session_id: str | None = None, limit: int = 50) -> dict[str, Any]:
    runs = repo.list_runs(session_id, limit)
    return {
        "runs": [
            {
                "id": r.id,
                "status": str(r.status),
                "score": r.score.overall,
                "passed": r.score.passed,
                "failed": r.score.failed,
                "errored": r.score.errored,
                "checks": len(r.results),
                "duration_ms": r.duration_ms,
                "narrative": r.narrative,
            }
            for r in runs
        ]
    }


@router.get("/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    run = repo.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    return run.model_dump(mode="json")


@router.get("/{run_id}/report")
def get_report(run_id: str, fmt: str = "markdown") -> dict[str, Any]:
    run = repo.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    if fmt == "json":
        from app.agent.tools.report import structured_summary

        return {"run_id": run_id, "summary": structured_summary(run), "results": [
            r.model_dump(mode="json") for r in run.results
        ]}
    return {
        "run_id": run_id,
        "markdown": run.narrative or "",
        "score": run.score.overall,
    }


@router.get("/{run_id}/export")
def export(run_id: str) -> Response:
    import csv
    import io

    run = repo.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "check", "table", "column", "kind", "severity", "status",
            "observed", "expected", "failed_rows", "message", "duration_ms",
        ]
    )
    for r in run.results:
        w.writerow(
            [
                r.name, r.table.fqn, r.column, r.kind, r.severity, r.status,
                r.observed, r.expected, r.failed_rows, r.message, r.duration_ms,
            ]
        )
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{run_id}-results.csv"'},
    )


@router.post("/{run_id}/cancel")
def cancel(run_id: str) -> dict[str, str]:
    run = repo.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    run.status = RunStatus.CANCELLED
    repo.save_run(run)
    return {"run_id": run_id, "status": str(run.status)}
