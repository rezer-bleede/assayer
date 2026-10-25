"""Chat API: WebSocket stream + session/message REST.

The conversation is a state machine, not a script:

    need_etl -> analyzing -> need_sources -> ready -> awaiting_approval -> running -> done

The agent always knows what it is missing and asks for exactly that, one
question at a time. Questions are generated deterministically (no LLM needed)
and delivered as `agent.question` events the UI renders as real controls.

Credentials never travel through chat text when the UI has a structured form:
the form POSTs to REST and then hands back an id, because chat messages are
persisted and replayed to `GET /messages`. Free-text pasting still works, and
what it detects is redacted before storage.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sqlalchemy import select

from app.agent.plan_builder import build_plan
from app.agent.planner import find_unresolved
from app.core.identity import user_id_context, workspace_id_context
from app.core.logging import log
from app.core.routes import SafeAPIRoute
from app.parsers import analyze_etl
from app.schemas import (
    ChatMessage,
    ChatRole,
    ConnectionSpec,
    ConversationStage,
    EtlAnalysis,
    EtlSubmit,
    Question,
    QuestionKind,
    QuestionOption,
    RunResult,
    RunStatus,
    SourceRequirement,
    SourceType,
    StreamEvent,
    TestPlan,
    new_id,
)
from app.services.connection_repair import (
    RepairReport,
    list_server_databases,
    probe,
    probe_attempt,
    repair_connection,
)
from app.services.ingest import engine_word_guess, extract_connections_async, looks_like_code
from app.services.orchestrator import build_bindings, introspect_all, profile_focus, run_audit
from app.services.projects import project_context_for_session
from app.services.source_filter import profile_summary
from app.store import db as db_store
from app.store import repository as repo
from app.store.db import LoginSessionRow, UserRow, WorkspaceMemberRow, WorkspaceRow, utcnow_str

router = APIRouter(tags=["chat"], route_class=SafeAPIRoute)

# words that mean "go" when a plan is already on file
AFFIRMATIVE = {
    "run the tests",
    "run tests",
    "run",
    "go",
    "audit",
    "start",
    "proceed",
    "run the audit",
    "test it",
    "check it",
    "run it",
    "yes",
    "start the audit",
    "ok",
    "okay",
    "approve",
    "looks good",
    "lgtm",
}

# words that adopt a *repaired* connection, never confused with plan approval
ACCEPT_CONNECTION = {
    "use it",
    "use that",
    "use those",
    "use this",
    "accept it",
    "accept",
    "switch to it",
    "yes use it",
}


# --------------------------------------------------------------------------- #
# REST
# --------------------------------------------------------------------------- #


class CreateSessionRequest(BaseModel):
    session_id: str | None = None


@router.post("/api/sessions")
def create_session(req: CreateSessionRequest) -> dict[str, Any]:
    sid = req.session_id or new_id()
    repo.ensure_session(sid)
    return {
        "session_id": sid,
        "messages": [],
        "stage": str(ConversationStage.NEED_ETL),
        "question": _need_etl_question().model_dump(mode="json"),
    }


@router.get("/api/sessions/{session_id}/messages")
def get_messages(session_id: str) -> dict[str, Any]:
    msgs = repo.list_messages(session_id)
    return {"messages": [m.model_dump(mode="json") for m in msgs]}


@router.get("/api/sessions/{session_id}/context")
def get_context(session_id: str) -> dict[str, Any]:
    """Everything needed to re-render the conversation after a reload."""
    ctx = repo.load_session_payload(session_id)
    return {
        "stage": ctx.get("stage", str(ConversationStage.NEED_ETL)),
        "connections": [c.redacted() for c in repo.list_connections(session_id)],
        "etl_id": ctx.get("etl_id"),
        "pending_plan": ctx.get("pending_plan"),
        "pending_question": ctx.get("question"),
        "requirements": ctx.get("requirements", []),
        "answered": ctx.get("answered", {}),
        "last_run_id": ctx.get("last_run_id"),
    }


# --------------------------------------------------------------------------- #
# questions
# --------------------------------------------------------------------------- #


def _need_etl_question() -> Question:
    return Question(
        stage=ConversationStage.NEED_ETL,
        title="Which codebase should I audit?",
        prompt=(
            "Point me at the transformation logic and I'll work out the lineage, "
            "figure out which databases it touches, then ask you for just those "
            "connections."
        ),
        options=[
            QuestionOption(
                kind=QuestionKind.GIT_URL,
                label="Git repository URL",
                help="Cloned read-only over HTTPS; we parse the files, never run them.",
                placeholder="https://github.com/acme/analytics.git",
                fields=["url", "ref", "token"],
                preferred=True,
            ),
            QuestionOption(
                kind=QuestionKind.FILE_UPLOAD,
                label="Upload source files",
                help="Select .sql / .py / dbt YAML files, or a whole folder.",
                preferred=True,
            ),
            QuestionOption(
                kind=QuestionKind.WEB_TARGET,
                label="Test a web app",
                help="Run browser checks on an app URL, with optional basic, header, or form-login authentication.",
                placeholder="https://app.example.com/dashboard",
                preferred=False,
            ),
            QuestionOption(
                kind=QuestionKind.PASTE,
                label="Paste code",
                help="Works for a single script, but a repo gives me far more context.",
                preferred=False,
            ),
        ],
    )


def _source_question(
    req: SourceRequirement, index: int, total: int, raw_code: str = ""
) -> Question:
    role = "writes to" if req.is_target_side else "reads from"
    tables = ", ".join(f"`{t}`" for t in req.tables[:6])
    more = f" (+{len(req.tables) - 6} more)" if len(req.tables) > 6 else ""

    # the prefill is what the UI echoes as "I expect:" chips — it must only
    # ever contain evidence, never a fabricated default. A declared engine
    # (profiles.yml / engine url) is strong; a bare word mention in the code
    # (engine_word_guess) is weak but real; anything else pre-fills nothing.
    prefill: dict[str, Any] = {}
    engine = req.engine_hint or engine_word_guess(raw_code)
    if engine:
        prefill["type"] = str(engine)
    for key, value in (
        ("host", req.host_hint),
        ("port", req.port_hint),
        ("database", req.database),
        ("username", req.user_hint),
        ("schema", req.schema_),
    ):
        if value:
            prefill[key] = value

    evidence = {
        "dbt profiles": "your dbt profiles.yml",
        "dbt sources.yml": "the sources declared in your dbt project",
        "engine url in code": "a connection string inside your own code",
        "sql schema prefix": "the schema prefixes in your SQL",
    }.get(req.origin, "the table names")

    return Question(
        stage=ConversationStage.NEED_SOURCES,
        title=f"Where does {req.tables[0].split('.')[-1]} live? ({index}/{total})",
        prompt=(
            f"The ETL {role} {tables}{more}. I grouped these by {evidence} "
            f"({req.describe()}); confidence {int(req.confidence * 100)}%."
        ),
        options=[
            QuestionOption(
                kind=QuestionKind.CONNECTION,
                label="Connection URL or details",
                help="Credentials are stored as secrets and never shown back to you.",
                placeholder="scheme://user:password@host:port/database",
                fields=["url"],
                prefill=prefill,
            )
        ],
        requirement_key=req.key,
        skip_allowed=total > 1,
    )


def _answered(requirement: SourceRequirement, ctx: dict, specs: dict[str, ConnectionSpec]) -> bool:
    """Is this requirement already satisfied by a connection we hold?"""
    answered = ctx.get("answered") or {}
    if answered.get(requirement.key):
        return True
    if requirement.key in (ctx.get("skipped") or []):
        return True

    want = {t.split(".")[-1].lower() for t in requirement.tables}
    for cid in ctx.get("connection_ids", []):
        spec = specs.get(cid)
        if spec is None:
            continue
        options = spec.options or {}
        declared = {str(t).split(".")[-1].lower() for t in options.get("tables", [])}
        if declared & want:
            return True
        if (
            requirement.database
            and spec.database
            and (spec.database.lower() == requirement.database.lower())
        ):
            return True
    return False


# --------------------------------------------------------------------------- #
# turn handling
# --------------------------------------------------------------------------- #


class TurnOutcome:
    def __init__(self) -> None:
        self.reply: str = ""
        self.stage: str = str(ConversationStage.NEED_ETL)
        self.question: Question | None = None
        self.plan: TestPlan | None = None
        self.awaiting_approval: bool = False
        # set when part of the reply was streamed mid-turn; the ws layer then
        # reuses the same id for the final persisted message so the client can
        # replace its streaming bubble instead of appending a duplicate
        self.streamed_id: str | None = None


class _StreamedReply:
    """Emit `message.start`/`message.delta` frames through the event buffer.

    The LLM reports prose through this sink: the first delta opens the frame,
    everything after appends. Emits stay synchronous (`EventBuffer` backs
    them), so they can be passed straight into `astream`/`run_audit`.
    """

    def __init__(self, emit: Any) -> None:
        self._emit = emit
        self.id = new_id()
        self.opened = False

    def __call__(self, text: str) -> None:
        if not self.opened:
            self.opened = True
            self._emit("message.start", {"id": self.id, "role": str(ChatRole.ASSISTANT)})
        self._emit("message.delta", {"id": self.id, "text": text})


class _ThinkingBurst:
    """Emit `thought.open`/`thought.delta`/`thought.close` frames for one LLM call.

    The model's private reasoning streams through `on_thought` (this sink) and
    is shown as an expandable "Thinking…" panel; when the call ends, `close`
    collapses it client-side with the seconds it took. Bursts are live-only:
    the burst frames are never persisted, and the final `message` frame stays
    untouched, so a reload never replays the reasoning.
    """

    def __init__(self, emit: Any) -> None:
        self._emit = emit
        self.id = new_id()
        self._t0: float | None = None
        self._opened = False
        self._closed = False
        self._pending: list[str] = []

    def __call__(self, text: str) -> None:
        if not text or self._closed:
            return
        if not self._opened:
            if not text.strip():  # leading blanks are not the start of thinking
                return
            self._opened = True
            self._t0 = time.time()
            self._emit("thought.open", {"id": self.id})
        self._pending.append(text)
        self._emit("thought.delta", {"id": self.id, "text": text})

    def text(self) -> str:
        """Everything thought so far (call items on the client side too)."""
        return "".join(self._pending)

    def close(self) -> None:
        """Collapse the burst with the seconds it took; no-op if never opened."""
        if not self._opened or self._closed:
            return
        self._closed = True
        secs = round(time.time() - (self._t0 or time.time()), 1)
        self._emit("thought.close", {"id": self.id, "secs": secs})


async def _absorb_pasted_code(text: str, session_id: str, emit: Any) -> tuple[str | None, str]:
    """Parse a pasted script if this message is one. Returns (etl_id, note)."""
    if not (looks_like_code(text) or text.strip().startswith("{")):
        return None, ""
    try:
        analysis = analyze_etl(
            EtlSubmit(language="sql", code=text, label="chat-paste", origin="paste")
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("chat etl parse failed", error=str(exc))
        return None, f"I couldn't parse that as ETL code ({type(exc).__name__})."

    repo.save_etl(analysis, session_id)
    emit("etl.analyzed", _etl_payload(analysis))
    return analysis.id, (
        f"I read that as {analysis.language}: {len(analysis.sources)} source(s) → "
        f"{len(analysis.targets)} target(s), {len(analysis.transformations)} transformation(s), "
        f"{len(analysis.required_sources)} database(s) to connect to."
    )


def _etl_payload(analysis: EtlAnalysis) -> dict[str, Any]:
    return {
        "etl_id": analysis.id,
        "language": str(analysis.language),
        "origin": str(analysis.origin),
        "repo_url": analysis.repo_url,
        "files": analysis.files[:40],
        "sources": analysis.sources,
        "targets": analysis.targets,
        "transformations": [
            {"kind": t.kind, "description": t.description} for t in analysis.transformations[:20]
        ],
        "risks": analysis.risks,
        "data_profiles": [
            {
                **p.model_dump(mode="json"),
                "columns": p.columns[:20],
            }
            for p in analysis.data_profiles[:20]
        ],
        "required_sources": [r.model_dump(mode="json") for r in analysis.required_sources],
    }


async def handle_turn(
    session_id: str,
    text: str,
    emit: Any,
    action: str | None = None,
    payload: dict[str, Any] | None = None,
    project_context: str | None = None,
) -> TurnOutcome:
    """Drive one turn of the state machine."""
    out = TurnOutcome()
    ctx = repo.load_session_payload(session_id)
    payload = payload or {}
    notes: list[str] = []
    if project_context is None:
        project_context = project_context_for_session(session_id)

    # ---- 1. structured handoffs from REST-driven UI ----------------------- #
    if action == "etl_provided" and payload.get("etl_id"):
        analysis = repo.get_etl(payload["etl_id"])
        if analysis:
            ctx["etl_id"] = analysis.id
            ctx["requirements"] = [r.model_dump(mode="json") for r in analysis.required_sources]
            emit("etl.analyzed", _etl_payload(analysis))
            note = (
                f"Parsed {analysis.label or 'the codebase'} as {analysis.language}: "
                f"{len(analysis.files)} file(s), {len(analysis.sources)} source table(s), "
                f"{len(analysis.targets)} target(s)."
            )
            profiled = profile_summary(analysis.data_profiles)
            if profiled:
                note = f"{note} {profiled}"
            notes.append(note)

    if action == "connection_provided" and payload.get("connection_id"):
        cid = payload["connection_id"]
        ids = list(ctx.get("connection_ids", []))
        if cid not in ids:
            ids.append(cid)
        ctx["connection_ids"] = ids
        if payload.get("requirement_key"):
            ctx.setdefault("answered", {})[payload["requirement_key"]] = cid
        emit(
            "connection.detected",
            {"connection_id": cid, "requirement_key": payload.get("requirement_key")},
        )

    if action == "skip_question" and payload.get("requirement_key"):
        # honour it, but remember the requirement is unmet so the plan may be thin
        ctx.setdefault("skipped", []).append(payload["requirement_key"])

    if action is None and text.strip().lower() == "skip" and ctx.get("question"):
        key = (ctx.get("question") or {}).get("requirement_key")
        if key:
            ctx.setdefault("skipped", []).append(key)
            notes.append("Skipped that source; the plan may not cover it.")

    # ---- 1b. adopt a connection the agent had to repair ------------------- #
    adopted = False
    if _accepts_pending_connection(ctx, action, text):
        adopted_note = _adopt_pending_connection(ctx, session_id, emit)
        if adopted_note:
            notes.append(adopted_note)
            adopted = True

    # ---- 2. free text: absorb anything recognisable ----------------------- #
    if action != "approve" and not adopted:
        for spec in await extract_connections_async(text):
            absorbed = await _absorb_connection(spec, session_id, ctx, emit)
            if absorbed:
                notes.append(absorbed)

        pasted_id, note = await _absorb_pasted_code(text, session_id, emit)
        if pasted_id:
            ctx["etl_id"] = pasted_id
            analysis = repo.get_etl(pasted_id)
            if analysis:
                ctx["requirements"] = [r.model_dump(mode="json") for r in analysis.required_sources]
        if note:
            notes.append(note)

    # ---- 3. approval executes, it never re-plans -------------------------- #
    if action == "approve" or (text.strip().lower() in AFFIRMATIVE and ctx.get("pending_plan")):
        executed = await _execute(ctx, session_id, emit)
        if executed is not None:
            out.reply, out.stage, out.awaiting_approval, out.streamed_id = executed
            _persist(session_id, ctx)
            return out
        out.reply = (
            "There's no approved plan to run yet — give me the codebase first and I'll "
            "ask for whatever it needs."
        )
        out.stage = ctx.get("stage", str(ConversationStage.NEED_ETL))
        _persist(session_id, ctx)
        return out

    # ---- 4. decide what to ask next -------------------------------------- #
    specs: dict[str, ConnectionSpec] = {}
    for cid in ctx.get("connection_ids", []):
        spec = repo.get_connection(cid)
        if spec:
            specs[cid] = spec

    analysis = repo.get_etl(ctx["etl_id"]) if ctx.get("etl_id") else None

    if analysis is None:
        web_targets = {cid: spec for cid, spec in specs.items() if spec.type == SourceType.WEB}
        if web_targets:
            out.stage = str(ConversationStage.AWAITING_APPROVAL)
            planned = await _plan(None, web_targets, ctx, session_id, emit, text, project_context)
            out.reply = (notes[0] + "\n\n" if notes and len(notes) == 1 else "") + planned
            _persist(session_id, ctx)
            return out
        out.stage = str(ConversationStage.NEED_ETL)
        out.question = _need_etl_question()
        out.reply = _first_line(notes) or (
            "Let's start with the transformation code." if not ctx.get("messages_seen") else ""
        )
        _emit_question(out.question, ctx, session_id, emit)
        _persist(session_id, ctx)
        return out

    outstanding = [r for r in analysis.required_sources if not _answered(r, ctx, specs)]
    if outstanding:
        out.stage = str(ConversationStage.NEED_SOURCES)
        total = len(outstanding)
        out.question = _source_question(outstanding[0], 1, total, raw_code=analysis.raw_code)
        extra = (
            f" I'll need {total} connection{'s' if total > 1 else ''} in total."
            if total > 1
            else ""
        )
        out.reply = _first_line(notes) + extra
        _emit_question(out.question, ctx, session_id, emit)
        _persist(session_id, ctx)
        return out

    # ---- 5. everything we need: build the plan ---------------------------- #
    out.stage = str(ConversationStage.AWAITING_APPROVAL)
    _persist(session_id, ctx)
    planned = await _plan(analysis, specs, ctx, session_id, emit, text, project_context)
    out.reply = (notes[0] + "\n\n" if notes and len(notes) == 1 else "") + planned
    return out


def _first_line(notes: list[str]) -> str:
    return notes[-1] if notes else ""


def _accepts_pending_connection(ctx: dict, action: str | None, text: str) -> bool:
    return bool(ctx.get("pending_connection")) and (
        action == "accept_connection" or (not action and text.strip().lower() in ACCEPT_CONNECTION)
    )


def _adopt_pending_connection(ctx: dict, session_id: str, emit: Any) -> str | None:
    """Save the repaired connection the user just confirmed."""
    pending = ctx.pop("pending_connection", None) or {}
    spec = ConnectionSpec.model_validate(pending.get("spec") or {})
    rec = repo.save_connection(spec, session_id)
    ctx["connection_ids"] = [*ctx.get("connection_ids", []), rec.id]
    if pending.get("requirement_key"):
        ctx.setdefault("answered", {})[pending["requirement_key"]] = rec.id
    emit("connection.detected", {"connection_id": rec.id, "spec": spec.redacted()})
    where = f"{spec.host}:{spec.port}/{spec.database}"
    return f"Using the repaired connection: {where}."


async def _absorb_connection(
    spec: ConnectionSpec, session_id: str, ctx: dict, emit: Any
) -> str | None:
    """Probe a connection the user gave us, repairing its addressing if needed.

    A connection that works as given is saved immediately. One the agent had to
    *repair* is never saved silently: it is parked as a pending suggestion for
    the user to confirm, because a guess drawn from a stale ETL hint must not
    quietly become the connection of record.
    """
    if spec.type == SourceType.WEB:
        status, _, _ = await asyncio.to_thread(probe, spec)
        if not status.ok:
            return f"I couldn't reach the web app at {spec.url}: {status.message or 'connection failed'}."
        rec = repo.save_connection(spec, session_id)
        ctx["connection_ids"] = [*ctx.get("connection_ids", []), rec.id]
        emit("connection.detected", {"connection_id": rec.id, "spec": spec.redacted()})
        return f"Captured the web app at {spec.url}."
    tables = list(spec.options.get("tables", []))
    result = await repair_connection(
        spec,
        attempt=lambda cand: probe_attempt(cand, tables),
        list_databases=list_server_databases,
        emit=emit,
    )
    where = f"{spec.host}:{spec.port}/{spec.database}" if spec.host else spec.name

    if not result.ok:
        emit("connection.repair.exhausted", result.report().model_dump(mode="json"))
        return (
            f"I couldn't reach {where} and tried a few alternatives — tell me the "
            "right host, port and database and I'll try again."
        )

    if result.repaired and result.suggested is not None:
        report: RepairReport = result.report()
        ctx["pending_connection"] = {
            "spec": result.suggested.model_dump(mode="json"),
            "report": report.model_dump(mode="json"),
        }
        suggested = result.suggested
        emit("connection.repair.suggested", report.model_dump(mode="json"))
        return (
            f"{where} didn't work, but this addressing does — "
            f"{suggested.host}:{suggested.port}/{suggested.database}. "
            "Say \u201cuse it\u201d and I'll switch to it."
        )

    rec = repo.save_connection(spec, session_id)
    ctx["connection_ids"] = [*ctx.get("connection_ids", []), rec.id]
    emit("connection.detected", {"connection_id": rec.id, "spec": spec.redacted()})
    return f"Captured a {spec.type} connection to {spec.host or spec.name}."


def _emit_question(question: Question | None, ctx: dict, session_id: str, emit: Any) -> None:
    if question is None:
        return
    ctx["question"] = question.model_dump(mode="json")
    emit("agent.question", question.model_dump(mode="json"))


def _persist(session_id: str, ctx: dict) -> None:
    ctx["stage_updated_at"] = utcnow_str()
    repo.update_session_payload(session_id, ctx)


async def _plan(
    analysis: EtlAnalysis | None,
    specs: dict[str, ConnectionSpec],
    ctx: dict,
    session_id: str,
    emit: Any,
    user_intent: str,
    project_context: str | None = None,
) -> str:
    if not specs:
        return (
            "I've read the code but I have no live connection to check it against. "
            "Once you give me the database details I'll profile the tables and draft the plan."
        )

    emit("plan.building", {"connections": len(specs)})
    introspected = introspect_all(specs, emit=emit)
    bindings = build_bindings(analysis, specs, introspected)
    missing = find_unresolved(analysis, bindings)
    profiles = profile_focus(
        specs, introspected, [(b.schema, b.table) for b in bindings], emit=emit
    )
    emit("thought", {"text": "Drafting the quality plan…"})
    burst = _ThinkingBurst(emit)
    plan = await build_plan(
        analysis,
        bindings,
        user_intent=user_intent[:300],
        profiles=profiles,
        on_thought=burst,
        project_context=project_context,
        connections=specs,
    )
    burst.close()

    if not plan.checks:
        if analysis is None:
            return "I couldn't create UI checks for that target. Check the URL and try again."
        ctx["stage"] = str(ConversationStage.NEED_SOURCES)
        wanted = ", ".join(sorted({*analysis.sources, *analysis.targets}))
        return (
            f"I reached {', '.join(s.name for s in specs.values())} but none of the tables in "
            f"the ETL ({wanted}) are there. Which schema should I be looking in?"
        )

    plan.run_id = plan.id
    repo.save_plan(plan, session_id)
    repo.save_run(
        RunResult(id=plan.run_id, status=RunStatus.AWAITING_APPROVAL, plan=plan, analysis=analysis),
        session_id,
    )
    ctx["pending_plan"] = plan.id
    ctx["question"] = None
    ctx["missing_tables"] = missing
    ctx["stage"] = str(ConversationStage.AWAITING_APPROVAL)
    _persist(session_id, ctx)

    emit(
        "plan.ready",
        {
            "run_id": plan.run_id,
            "summary": plan.summary,
            "missing_tables": missing,
            "checks": [
                {
                    "id": c.id,
                    "name": c.name,
                    "kind": str(c.kind),
                    "table": c.table.fqn if c.table else (c.url or "web app"),
                    "column": c.column,
                    "severity": str(c.severity),
                    "rationale": c.rationale,
                }
                for c in plan.checks
            ],
        },
    )
    emit("plan.awaiting_approval", {"run_id": plan.run_id})

    top = [
        f"- {c.name} ({c.severity}) on `{c.table.fqn if c.table else (c.url or 'web app')}`" + (f".{c.column}" if c.column else "")
        for c in plan.checks[:6]
    ]
    caveat = ""
    if missing:
        caveat = (
            "\n\nI could not find "
            + ", ".join(f"`{m}`" for m in missing[:6])
            + ("…" if len(missing) > 6 else "")
            + " in the connections you gave me, so those tables are not covered. "
            "Tell me where they live and I'll add them."
        )
    return (
        f"I profiled {len(specs)} source(s) and drafted **{len(plan.checks)} checks**. "
        f"Strategy: {plan.summary}\n\nTop checks:\n"
        + "\n".join(top)
        + caveat
        + "\n\nApprove to run them, or tell me what to add or drop."
    )


async def _execute(
    ctx: dict, session_id: str, emit: Any
) -> tuple[str, str, bool, str | None] | None:
    plan = repo.get_plan(ctx.get("pending_plan") or "")
    if plan is None:
        return None

    specs: dict[str, ConnectionSpec] = {}
    for cid in ctx.get("connection_ids", []):
        spec = repo.get_connection(cid)
        if spec:
            specs[cid] = spec
    if not specs:
        return (
            "I still need a connection before I can run anything.",
            str(ConversationStage.NEED_SOURCES),
            False,
            None,
        )

    stream = _StreamedReply(emit)
    burst = _ThinkingBurst(emit)
    emit("run.starting", {"run_id": plan.run_id})
    emit("thought", {"text": f"Running {len(plan.checks)} checks against live data…"})
    analysis = repo.get_etl(ctx.get("etl_id") or "")
    result = await run_audit(
        plan.run_id,
        plan,
        specs,
        analysis=analysis,
        session_id=session_id,
        emit=emit,
        on_delta=stream,
        on_thought=burst,
    )
    burst.close()
    ctx["last_run_id"] = result.id
    ctx["pending_plan"] = None
    ctx["question"] = None
    ctx["stage"] = str(ConversationStage.DONE)
    emit("run.completed", result.model_dump(mode="json"))

    reply = (
        f"Audit finished: **{result.score.overall}/100** "
        f"({result.score.passed} passed, {result.score.failed} failed, "
        f"{result.score.errored} errored).\n\n{result.narrative or ''}"
    )
    return reply, str(ConversationStage.DONE), False, stream.id if stream.opened else None


def _is_affirmative(text: str) -> bool:
    return text.strip().lower() in AFFIRMATIVE


# --------------------------------------------------------------------------- #
# websocket
# --------------------------------------------------------------------------- #


@router.websocket("/ws/chat")
async def ws_chat(ws: WebSocket) -> None:
    token = ws.cookies.get("assayer_session")
    if not token:
        await ws.close(code=4401, reason="sign in required")
        return
    import hashlib

    with db_store.SessionLocal() as db:
        session = db.scalar(select(LoginSessionRow).where(
            LoginSessionRow.token_hash == hashlib.sha256(token.encode()).hexdigest()
        ))
        if session is None or session.expires_at <= utcnow_str() or db.get(UserRow, session.user_id) is None:
            await ws.close(code=4401, reason="session expired")
            return
        user_id = session.user_id
        workspace_id: str | None = None
        requested = ws.cookies.get("assayer_workspace")
        membership = db.scalar(select(WorkspaceMemberRow).where(
            WorkspaceMemberRow.user_id == user_id, WorkspaceMemberRow.workspace_id == requested
        )) if requested else None
        if membership:
            workspace_id = membership.workspace_id
        else:
            personal = db.scalar(select(WorkspaceRow).join(
                WorkspaceMemberRow, WorkspaceMemberRow.workspace_id == WorkspaceRow.id
            ).where(WorkspaceMemberRow.user_id == user_id, WorkspaceRow.kind == "personal"))
            workspace_id = personal.id if personal else None
    user_marker = user_id_context.set(user_id)
    workspace_marker = workspace_id_context.set(workspace_id)
    session_id: str | None = None

    async def send(event: str, data: dict[str, Any]) -> None:
        await ws.send_text(
            StreamEvent(type=event, session_id=session_id or "", data=data).model_dump_json()
        )

    try:
        await ws.accept()
        hello = await ws.receive_text()
        payload = json.loads(hello)
        session_id = payload.get("session_id")
        if not session_id:
            await ws.close(code=4400, reason="session_id required")
            return
        repo.ensure_session(session_id)

        ctx = repo.load_session_payload(session_id)
        stage = ctx.get("stage", str(ConversationStage.NEED_ETL))
        await send("session.ready", {"session_id": session_id, "stage": stage})

        history = repo.list_messages(session_id)
        await send("history", {"messages": [m.model_dump(mode="json") for m in history]})

        # a reload should land back on the question that was pending
        if ctx.get("question"):
            await send("agent.question", ctx["question"])
        elif not history:
            q = _need_etl_question()
            ctx["question"] = q.model_dump(mode="json")
            ctx["stage"] = str(ConversationStage.NEED_ETL)
            _persist(session_id, ctx)
            await send("agent.question", q.model_dump(mode="json"))

        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            text = str(msg.get("content") or "").strip()
            action = msg.get("action")
            body = msg.get("payload") or {}

            if text:
                # never persist a raw credential into replayable history
                user_msg = ChatMessage(role=ChatRole.USER, content=_redact_echo(text))
                repo.append_message(session_id, user_msg)
                await send("message", user_msg.model_dump(mode="json"))

            await send("thinking", {"started_at": time.time()})
            buf = EventBuffer()
            stop = asyncio.Event()
            pump = asyncio.create_task(buf.pump_forever(send, stop=stop))
            try:
                outcome = await handle_turn(session_id, text, emit=buf, action=action, payload=body)
            except Exception as exc:  # noqa: BLE001
                log.exception("turn failed")
                error = f"{type(exc).__name__}: {exc}"
                outcome = None
            else:
                error = None
            finally:
                # let the pump finish the frame it is writing, then stop it
                stop.set()
                with contextlib.suppress(asyncio.CancelledError):
                    await pump
            if error is not None:
                await send("error", {"message": error})
                continue
            await buf.flush(send)

            await send("stage", {"stage": outcome.stage})

            reply = ChatMessage(role=ChatRole.ASSISTANT, content=outcome.reply)
            if outcome.streamed_id:
                # same id the client watched stream in: the final frame replaces
                # its bubble instead of appending a duplicate
                reply = reply.model_copy(update={"id": outcome.streamed_id})
            repo.append_message(session_id, reply)
            await send("message", reply.model_dump(mode="json"))
            await send("done", {"stage": outcome.stage})

    except WebSocketDisconnect:
        log.info("ws disconnected", session_id=session_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("websocket error")
        with contextlib.suppress(Exception):
            await ws.close(code=1011, reason=str(exc)[:120])
    finally:
        user_id_context.reset(user_marker)
        workspace_id_context.reset(workspace_marker)


_CRED_ECHO = re.compile(r"(\w+://[^:/@\s]+:)([^@\s]+)(@)")


def _redact_echo(text: str) -> str:
    """Mask passwords inside a URL before the message is stored."""
    if len(text) > 4000:
        return text[:4000] + " …[truncated]"
    return _CRED_ECHO.sub(lambda m: f"{m.group(1)}***{m.group(3)}", text)


class EventBuffer:
    """Ordered, back-pressured event sink.

    Emitters run deep inside synchronous code, so they cannot await a send.
    Appending here and flushing with `await` guarantees the client sees events
    in causal order — fire-and-forget tasks do not.
    """

    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []

    def __call__(self, event: str, data: dict[str, Any]) -> None:
        self._events.append({"type": event, "data": data})

    emit = __call__

    def drain(self) -> list[dict[str, Any]]:
        out, self._events = self._events, []
        return out

    async def flush(self, send: Any) -> None:
        for ev in self.drain():
            await send(ev["type"], ev["data"])

    async def pump_forever(
        self,
        send: Any,
        interval: float = 0.05,
        stop: asyncio.Event | None = None,
    ) -> None:
        """Drain continuously so long turns stream progress live.

        `stop` asks the pump to finish the drain it is in and exit. Cancelling
        it instead would interrupt a write mid-frame and wedge the socket:
        every later frame would then be silently lost.
        """
        try:
            while True:
                await self.flush(send)
                if stop is None:
                    await asyncio.sleep(interval)
                    continue
                if stop.is_set():
                    return
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self.flush(send)
            raise
