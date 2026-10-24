"""Full audit orchestration shared by the chat loop and the REST API."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.agent.planner import SourceBinding, bind_sources
from app.agent.planner import _bare as bare
from app.agent.tools.introspect import introspect_source, profile_table
from app.agent.tools.report import build_report
from app.core.logging import log
from app.runner.executor import execute_plan
from app.schemas import (
    ConnectionSpec,
    EtlAnalysis,
    RunResult,
    RunStatus,
    TestPlan,
)
from app.store import repository as repo

Emit = Callable[[str, dict[str, Any]], Any]


def introspect_all(
    connections: dict[str, ConnectionSpec],
    emit: Emit | None = None,
    max_tables: int = 40,
) -> dict[str, list[dict]]:
    introspected: dict[str, list[dict]] = {}
    for cid, spec in connections.items():
        if emit:
            where = spec.host or spec.name
            emit("thought", {"text": f"Connecting to {where}…"})
        data = introspect_source(spec, max_tables=max_tables)
        if not data.get("ok"):
            if emit:
                emit("source.error", {"source_id": cid, "error": data.get("error")})
            continue
        introspected[cid] = data.get("tables", [])
        if emit:
            emit(
                "source.introspected",
                {"source_id": cid, "name": spec.name, "tables": len(introspected[cid])},
            )
            emit(
                "thought",
                {"text": f"Read {spec.name} — {len(introspected[cid])} tables found."},
            )
    return introspected


def profile_focus(
    connections: dict[str, ConnectionSpec],
    introspected: dict[str, list[dict]],
    focus: list[tuple[str | None, str]],
    emit: Emit | None = None,
) -> dict[str, dict]:
    """Profile exactly the tables the ETL touches.

    Profiling blindly would waste the budget on unrelated tables (including the
    app's own state store) and skip the ones that matter.
    """
    profiles: dict[str, dict] = {}
    wanted = {(s, t) for s, t in focus}
    for cid, spec in connections.items():
        tables = introspected.get(cid, [])
        picked = [
            t
            for t in tables
            if (t.get("schema"), t.get("name")) in wanted or (bare(t.get("name", "")),) in wanted
        ]
        for t in picked:
            if "error" in t or not t.get("name"):
                continue
            fqn = ".".join(x for x in (t.get("schema"), t.get("name")) if x)
            if emit:
                emit("thought", {"text": f"Profiling {fqn}…"})
            p = profile_table(spec, t.get("schema"), t.get("name"))
            if p.get("ok"):
                profiles[f"{cid}|{t['name']}"] = p
    if emit and profiles:
        emit("sources.profiled", {"tables": list(profiles.keys())})
    return profiles


def profile_all(
    connections: dict[str, ConnectionSpec],
    emit: Emit | None = None,
    max_tables: int = 40,
    profile_limit: int = 8,
) -> tuple[dict[str, list[dict]], dict[str, dict]]:
    """Introspect + profile the first `profile_limit` tables per source."""
    introspected = introspect_all(connections, emit=emit, max_tables=max_tables)
    focus: list[tuple[str | None, str]] = []
    for tables in introspected.values():
        for t in tables[:profile_limit]:
            if t.get("name") and "error" not in t:
                focus.append((t.get("schema"), t["name"]))
    return introspected, profile_focus(connections, introspected, focus, emit=emit)


async def run_audit(
    run_id: str,
    plan: TestPlan,
    connections: dict[str, ConnectionSpec],
    analysis: EtlAnalysis | None = None,
    session_id: str | None = None,
    emit: Emit | None = None,
    on_delta: Callable[[str], None] | None = None,
    on_thought: Callable[[str], None] | None = None,
) -> RunResult:
    """Execute the approved plan, score it, and write the narrative.

    `on_delta`, when given, receives report-narrative tokens as they stream
    from the LLM, so the chat can show prose while it is still being written.
    `on_thought` receives the model's private reasoning tokens the same way.
    """
    plan.run_id = run_id
    pending = RunResult(id=run_id, status=RunStatus.RUNNING, plan=plan, analysis=analysis)
    repo.save_run(pending, session_id)

    def progress(event: str, data: dict[str, Any]) -> None:
        if emit:
            emit(event, data)

    result = await execute_plan(run_id, plan, connections, progress=progress)
    result.analysis = analysis
    if emit:
        emit("thought", {"text": "All checks done — writing the report…"})
    from app.services.projects import project_context_for_session

    project_context = project_context_for_session(session_id) if session_id else None
    result.narrative = await build_report(
        result,
        analysis,
        on_delta=on_delta,
        on_thought=on_thought,
        project_context=project_context,
    )

    try:
        repo.save_run(result, session_id)
    except Exception as exc:  # noqa: BLE001
        # Never discard a completed audit because the state store blinked; the
        # caller still gets its results, flagged so the gap is not silent.
        log.error("failed to persist run", run_id=run_id, error=str(exc))
        note = (
            f"\n\n> ⚠️ Results could not be saved to the audit store "
            f"({type(exc).__name__}). They are shown here but will not appear in history."
        )
        result.narrative = (result.narrative or "") + note

    log.info(
        "audit complete", run_id=run_id, score=result.score.overall, failed=result.score.failed
    )
    return result


def build_bindings(
    analysis: EtlAnalysis | None,
    connections: dict[str, ConnectionSpec],
    introspected: dict[str, list[dict]],
) -> list[SourceBinding]:
    if analysis is None:
        return []
    return bind_sources(analysis, connections, introspected)
