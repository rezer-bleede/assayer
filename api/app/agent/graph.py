"""LangGraph orchestrator for the audit flow.

    intake -> profile -> plan -> [human approval] -> execute -> evaluate

Graph state is a plain TypedDict; the node functions call the same services the
REST API uses, so the chat UI and the API stay behaviourally identical.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from app.agent.plan_builder import build_plan
from app.agent.planner import SourceBinding, bind_sources
from app.agent.tools.introspect import introspect_source, profile_table
from app.agent.tools.report import build_report
from app.core.llm import ainvoke, llm_available
from app.core.logging import log
from app.schemas import (
    ConnectionSpec,
    EtlAnalysis,
    RunResult,
    TestPlan,
)


class AgentState(TypedDict, total=False):
    session_id: str
    connections: dict[str, ConnectionSpec]
    analysis: EtlAnalysis | None
    introspected: dict[str, list[dict]]
    profiles: dict[str, dict]
    bindings: list[dict]
    plan: TestPlan | None
    run: RunResult | None
    report: str | None
    user_intent: str | None
    error: str | None
    approved: bool
    events: Annotated[list[dict], lambda a, b: a + b]


def make_emitter(events: list[dict]):
    def emit(kind: str, payload: dict) -> None:
        events.append({"type": kind, "data": payload})

    return emit


# --------------------------------------------------------------------------- #
# nodes
# --------------------------------------------------------------------------- #


def node_profile(state: AgentState, emit=make_emitter([])) -> AgentState:
    """Introspect every connection and bind ETL table refs to real sources."""
    emit("node.profile", {"status": "running", "connections": len(state["connections"])})
    introspected: dict[str, list[dict]] = {}
    profiles: dict[str, dict] = {}

    for cid, spec in state["connections"].items():
        data = introspect_source(spec, max_tables=40)
        if data.get("ok"):
            introspected[cid] = data.get("tables", [])
            emit(
                "source.introspected",
                {"source_id": cid, "name": spec.name, "tables": len(introspected[cid])},
            )
            for t in introspected[cid][:6]:
                if "error" in t:
                    continue
                p = profile_table(spec, t.get("schema"), t.get("name"))
                if p.get("ok"):
                    profiles[f"{cid}|{t['name']}"] = p
        else:
            emit("source.error", {"source_id": cid, "error": data.get("error")})
            log.warning("introspection failed", source=spec.name, error=data.get("error"))

    bindings = bind_sources(state["analysis"] or _noop_analysis(), state["connections"], introspected)
    emit(
        "lineage.bound",
        {"bound": [f"{b.source_id}:{b.schema or '.'}{b.table}" for b in bindings]},
    )
    return {**state, "introspected": introspected, "profiles": profiles,
            "bindings": [b.__dict__ if not hasattr(b, "__slots__") else _bd(b) for b in bindings]}


def _bd(b: SourceBinding) -> dict:
    return {"source_id": b.source_id, "name": b.name, "schema": b.schema, "table": b.table,
            "is_target": b.is_target, "row_count": b.row_count, "primary_keys": b.primary_keys,
            "columns": b.columns}


def _bindings(state: AgentState) -> list[SourceBinding]:
    return [SourceBinding(**b) for b in state.get("bindings", [])]


async def node_plan(state: AgentState, emit=make_emitter([])) -> AgentState:
    """Design the test plan (deterministic baseline + LLM augmentation)."""
    emit("node.plan", {"status": "running"})
    bindings = _bindings(state)
    plan = await build_plan(
        state.get("analysis"),
        bindings,
        user_intent=state.get("user_intent"),
        use_llm=llm_available(),
    )
    emit(
        "plan.ready",
        {
            "plan_id": plan.id,
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
                for c in plan.checks
            ],
        },
    )
    return {**state, "plan": plan, "approved": False}


async def node_evaluate(state: AgentState, emit=make_emitter([])) -> AgentState:
    """Turn raw results into an audit narrative."""
    run = state.get("run")
    if run is None:
        return {**state, "error": "no run to evaluate"}
    emit("node.evaluate", {"status": "running"})
    report = await build_report(run, state.get("analysis"))
    emit("report.ready", {"run_id": run.id, "report": report, "score": run.score.overall})
    return {**state, "report": report}


def route_after_plan(state: AgentState) -> Literal["execute", "await_approval"]:
    return "execute" if state.get("approved") else "await_approval"


# --------------------------------------------------------------------------- #
# graph
# --------------------------------------------------------------------------- #


def build_graph(emit=make_emitter([])):
    g = StateGraph(AgentState)
    g.add_node("profile", lambda s: node_profile(s, emit))
    g.add_node("plan", lambda s: node_plan(s, emit))
    g.add_node("evaluate", lambda s: node_evaluate(s, emit))
    g.add_node("await_approval", lambda s: s)

    g.add_edge(START, "profile")
    g.add_edge("profile", "plan")
    g.add_edge("plan", "await_approval")
    g.add_conditional_edges(
        "await_approval", route_after_plan, {"execute": END, "await_approval": END}
    )
    return g.compile()


_GRAPH: Any = None


def get_graph() -> Any:
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    return _GRAPH


__all__ = ["AgentState", "build_graph", "get_graph", "node_profile", "node_plan", "node_evaluate"]


def _noop_analysis() -> EtlAnalysis:
    return EtlAnalysis(language="sql", targets=[], sources=[])


async def narrate_failure(run: RunResult, result: Any) -> str:
    from app.agent.prompts import EXPLAIN_CHECK_PROMPT

    prompt = EXPLAIN_CHECK_PROMPT.format(
        check=result.name,
        observed=result.observed,
        expected=result.expected,
        sample=result.sample_rows[:3] if result.sample_rows else "not captured",
    )
    if llm_available():
        try:
            return await ainvoke(prompt, max_tokens=300)
        except Exception as exc:  # noqa: BLE001
            log.warning("llm explain failed", error=str(exc))
    return (
        f"{result.name} failed: observed {result.observed}, expected {result.expected}. "
        f"{result.message or ''}".strip()
    )