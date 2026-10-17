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

from app.agent.plan_builder import build_plan
from app.agent.planner import find_unresolved
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
    StreamEvent,
    TestPlan,
)
from app.services.ingest import extract_connections, looks_like_code
from app.services.orchestrator import build_bindings, introspect_all, profile_focus, run_audit
from app.services.source_filter import profile_summary
from app.store import repository as repo
from app.store.db import utcnow_str

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


# --------------------------------------------------------------------------- #
# REST
# --------------------------------------------------------------------------- #


class CreateSessionRequest(BaseModel):
    session_id: str | None = None


@router.post("/api/sessions")
def create_session(req: CreateSessionRequest) -> dict[str, Any]:
    from app.schemas import new_id

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
                kind=QuestionKind.PASTE,
                label="Paste code",
                help="Works for a single script, but a repo gives me far more context.",
                preferred=False,
            ),
        ],
    )


def _source_question(req: SourceRequirement, index: int, total: int) -> Question:
    role = "writes to" if req.is_target_side else "reads from"
    tables = ", ".join(f"`{t}`" for t in req.tables[:6])
    more = f" (+{len(req.tables) - 6} more)" if len(req.tables) > 6 else ""

    prefill = {"type": str(req.engine_hint) if req.engine_hint else "postgres"}
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
                placeholder="postgresql://user:password@host:5432/database",
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
) -> TurnOutcome:
    """Drive one turn of the state machine."""
    out = TurnOutcome()
    ctx = repo.load_session_payload(session_id)
    payload = payload or {}
    notes: list[str] = []

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

    # ---- 2. free text: absorb anything recognisable ----------------------- #
    if action != "approve":
        for spec in extract_connections(text):
            rec = repo.save_connection(spec, session_id)
            ctx["connection_ids"] = [*ctx.get("connection_ids", []), rec.id]
            emit("connection.detected", {"connection_id": rec.id, "spec": spec.redacted()})
            notes.append(f"Captured a {spec.type} connection to {spec.host or spec.name}.")

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
            out.reply, out.stage, out.awaiting_approval = executed
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
        out.question = _source_question(outstanding[0], 1, total)
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
    planned = await _plan(analysis, specs, ctx, session_id, emit, text)
    out.reply = (notes[0] + "\n\n" if notes and len(notes) == 1 else "") + planned
    return out


def _first_line(notes: list[str]) -> str:
    return notes[-1] if notes else ""


def _emit_question(question: Question | None, ctx: dict, session_id: str, emit: Any) -> None:
    if question is None:
        return
    ctx["question"] = question.model_dump(mode="json")
    emit("agent.question", question.model_dump(mode="json"))


def _persist(session_id: str, ctx: dict) -> None:
    ctx["stage_updated_at"] = utcnow_str()
    repo.update_session_payload(session_id, ctx)


async def _plan(
    analysis: EtlAnalysis,
    specs: dict[str, ConnectionSpec],
    ctx: dict,
    session_id: str,
    emit: Any,
    user_intent: str,
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
    profiles = profile_focus(specs, introspected, [(b.schema, b.table) for b in bindings])
    plan = await build_plan(analysis, bindings, user_intent=user_intent[:300], profiles=profiles)

    if not plan.checks:
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
                    "table": c.table.fqn,
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
        f"- {c.name} ({c.severity}) on `{c.table.fqn}`" + (f".{c.column}" if c.column else "")
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
        f"Strategy: {plan.summary}\n\nTop checks:\n" + "\n".join(top)
        + caveat
        + "\n\nApprove to run them, or tell me what to add or drop."
    )


async def _execute(ctx: dict, session_id: str, emit: Any) -> tuple[str, str, bool] | None:
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
        )

    emit("run.starting", {"run_id": plan.run_id})
    analysis = repo.get_etl(ctx.get("etl_id") or "")
    result = await run_audit(
        plan.run_id, plan, specs, analysis=analysis, session_id=session_id, emit=emit
    )
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
    return reply, str(ConversationStage.DONE), False


def _is_affirmative(text: str) -> bool:
    return text.strip().lower() in AFFIRMATIVE


# --------------------------------------------------------------------------- #
# websocket
# --------------------------------------------------------------------------- #


@router.websocket("/ws/chat")
async def ws_chat(ws: WebSocket) -> None:
    await ws.accept()
    session_id: str | None = None

    async def send(event: str, data: dict[str, Any]) -> None:
        await ws.send_text(
            StreamEvent(type=event, session_id=session_id or "", data=data).model_dump_json()
        )

    try:
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
            pump = asyncio.create_task(buf.pump_forever(send))
            try:
                outcome = await handle_turn(session_id, text, emit=buf, action=action, payload=body)
            except Exception as exc:  # noqa: BLE001
                log.exception("turn failed")
                await send("error", {"message": f"{type(exc).__name__}: {exc}"})
                continue
            finally:
                pump.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pump
            await buf.flush(send)

            await send("stage", {"stage": outcome.stage})

            reply = ChatMessage(role=ChatRole.ASSISTANT, content=outcome.reply)
            repo.append_message(session_id, reply)
            await send("message", reply.model_dump(mode="json"))
            await send("done", {"stage": outcome.stage})

    except WebSocketDisconnect:
        log.info("ws disconnected", session_id=session_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("websocket error")
        with contextlib.suppress(Exception):
            await ws.close(code=1011, reason=str(exc)[:120])


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

    async def pump_forever(self, send: Any, interval: float = 0.05) -> None:
        """Drain continuously so long turns stream progress live."""
        try:
            while True:
                await self.flush(send)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            await self.flush(send)
            raise
