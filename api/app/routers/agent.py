"""AG-UI SSE endpoint for the OpenUI AgentInterface."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent.openui import program_for_state, state_snapshot
from app.agent.openui_author import author_program
from app.core.logging import log
from app.core.routes import SafeAPIRoute
from app.routers.chat import EventBuffer, handle_turn
from app.schemas import ChatMessage, ChatRole, ConnectionSpec, Question, RunResult, TestPlan, new_id
from app.store import repository as repo

router = APIRouter(tags=["agent"], route_class=SafeAPIRoute)


class AgentRequest(BaseModel):
    threadId: str
    runId: str | None = None
    messages: list[dict[str, Any]] = Field(default_factory=list)
    tools: list[Any] = Field(default_factory=list)
    context: list[Any] = Field(default_factory=list)


_CONTENT_RE = re.compile(
    r"^\]\]>openui:content(?:\?[^\n]*)?\r?\n(.*?)(?=^\]\]>openui:(?:context|scripts|end)|\Z)",
    re.MULTILINE | re.DOTALL,
)
_CONTEXT_RE = re.compile(
    r"^\]\]>openui:context(?:\?[^\n]*)?\r?\n(.*?)(?=^\]\]>openui:|\Z)",
    re.MULTILINE | re.DOTALL,
)


def clean_user_text(raw: str) -> str:
    """Unwrap OpenUI's action envelope into the text the user actually sent.

    Component actions arrive as ``]]>openui:content`` plus a ``context``
    section; sending that raw string to the planner hides the intent ("run the
    tests", "skip", …) behind transport markers.
    """
    match = _CONTENT_RE.search(raw)
    if match:
        content = match.group(1).rstrip("\r\n")
        if content.strip():
            return content.strip()

    context = _CONTEXT_RE.search(raw)
    if context:
        with contextlib.suppress(Exception):
            parsed = json.loads(context.group(1).strip())
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, str) and item.strip():
                        text = item.strip()
                        return text.removeprefix("User clicked: ").strip()
    return raw.strip()


def _latest_user_text(messages: list[dict[str, Any]]) -> str:
    for raw in reversed(messages):
        if str(raw.get("role") or "") != "user":
            continue
        content = raw.get("content")
        if isinstance(content, str):
            return clean_user_text(content)
        if isinstance(content, list):
            bits = []
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    bits.append(part["text"])
            return clean_user_text("\n".join(bits))
    return ""


def _message(raw: dict[str, Any]) -> ChatMessage | None:
    role = str(raw.get("role") or "")
    if role not in {"user", "assistant", "system", "tool"}:
        return None
    content = raw.get("content")
    if not isinstance(content, str):
        content = "" if content is None else str(content)
    return ChatMessage(
        id=str(raw.get("id") or new_id()),
        role=ChatRole(role),
        content=content,
    )


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


def _load_state(
    session_id: str, reply: str
) -> tuple[
    dict[str, Any],
    Question | None,
    TestPlan | None,
    RunResult | None,
    Any,
    dict[str, ConnectionSpec],
]:
    ctx = repo.load_session_payload(session_id)
    question = None
    if ctx.get("question"):
        with contextlib.suppress(Exception):
            question = Question.model_validate(ctx["question"])
    plan = repo.get_plan(ctx.get("pending_plan") or "") if ctx.get("pending_plan") else None
    run = repo.get_run(ctx.get("last_run_id") or "") if ctx.get("last_run_id") else None
    analysis = repo.get_etl(ctx["etl_id"]) if ctx.get("etl_id") else None
    specs: dict[str, ConnectionSpec] = {}
    for cid in ctx.get("connection_ids", []):
        spec = repo.get_connection(cid)
        if spec:
            specs[cid] = spec
    return ctx, question, plan, run, analysis, specs


async def _turn(session_id: str, text: str) -> tuple[str, ChatMessage | None]:
    buf = EventBuffer()
    await asyncio.to_thread(repo.ensure_session, session_id)

    try:
        outcome = await handle_turn(session_id, text, emit=buf)
    except Exception as exc:  # noqa: BLE001
        log.exception("agent turn failed", session_id=session_id)
        raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc

    ctx, question, plan, run, analysis, specs = _load_state(session_id, outcome.reply)
    fallback = program_for_state(
        ctx=ctx,
        question=question,
        plan=plan,
        run=run,
        analysis=analysis,
        notes=[outcome.reply] if outcome.reply else None,
    )
    state = state_snapshot(
        ctx=ctx,
        question=question,
        plan=plan,
        run=run,
        analysis=analysis,
        specs=specs,
        notes=[outcome.reply] if outcome.reply else None,
    )
    program = await author_program(state, fallback)
    assistant = ChatMessage(role=ChatRole.ASSISTANT, content=program)
    repo.append_message(session_id, assistant)
    return program, assistant


@router.post("/agent")
async def agent(req: AgentRequest, request: Request) -> StreamingResponse:
    session_id = req.threadId or new_id()
    text = _latest_user_text(req.messages)
    message_id = new_id()

    if text:
        latest = next(
            (raw for raw in reversed(req.messages) if str(raw.get("role") or "") == "user"),
            None,
        )
        if latest is not None:
            parsed = _message(latest)
            if parsed is not None:
                parsed.content = text
                repo.append_message(session_id, parsed)

    async def stream() -> AsyncIterator[str]:
        yield _sse({"type": "TEXT_MESSAGE_START", "messageId": message_id, "role": "assistant"})
        try:
            program, _ = await _turn(session_id, text)
        except Exception as exc:  # noqa: BLE001
            log.exception("agent stream failed", session_id=session_id)
            yield _sse(
                {
                    "type": "RUN_ERROR",
                    "message": f"The audit agent hit an error: {exc}",
                    "code": "agent_error",
                }
            )
            return
        yield _sse(
            {
                "type": "TEXT_MESSAGE_CONTENT",
                "messageId": message_id,
                "delta": program,
            }
        )
        yield _sse({"type": "TEXT_MESSAGE_END", "messageId": message_id})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
