"""High-level state access helpers used by routers and the agent graph."""

from __future__ import annotations

from typing import Any

from app.schemas import (
    ChatMessage,
    ConnectionRecord,
    ConnectionSpec,
    EtlAnalysis,
    RunResult,
    TestPlan,
)
from app.store.db import (
    ChatMessageRow,
    CheckResultRow,
    ConnectionRow,
    EtlRow,
    RunRow,
    SessionRow,
    utcnow_str,
    write_session,
)

# --------------------------------------------------------------------------- #
# sessions
# --------------------------------------------------------------------------- #


def ensure_session(session_id: str) -> None:
    with write_session() as s:
        exists = s.get(SessionRow, session_id)
        if not exists:
            s.add(SessionRow(id=session_id, created_at=utcnow_str(), payload={}))


def load_session_payload(session_id: str) -> dict[str, Any]:
    with write_session() as s:
        row = s.get(SessionRow, session_id)
        return dict(row.payload) if row else {}


def update_session_payload(session_id: str, payload: dict[str, Any]) -> None:
    with write_session() as s:
        row = s.get(SessionRow, session_id)
        if row is None:
            s.add(SessionRow(id=session_id, created_at=utcnow_str(), payload=payload))
        else:
            row.payload = payload


# --------------------------------------------------------------------------- #
# connections
# --------------------------------------------------------------------------- #


def save_connection(spec: ConnectionSpec, session_id: str | None = None) -> ConnectionRecord:
    rec = ConnectionRecord(spec=spec)
    with write_session() as s:
        s.add(
            ConnectionRow(
                id=rec.id,
                session_id=session_id,
                name=spec.name,
                type=str(spec.type),
                created_at=rec.created_at.isoformat(),
                payload=spec.model_dump(mode="json"),
            )
        )
    return rec


def get_connection(conn_id: str) -> ConnectionSpec | None:
    with write_session() as s:
        row = s.get(ConnectionRow, conn_id)
        return ConnectionSpec.model_validate(row.payload) if row else None


def list_connections(session_id: str | None = None) -> list[ConnectionSpec]:
    with write_session() as s:
        q = s.query(ConnectionRow)
        if session_id:
            q = q.filter(ConnectionRow.session_id == session_id)
        return [ConnectionSpec.model_validate(r.payload) for r in q.order_by(ConnectionRow.created_at)]


# --------------------------------------------------------------------------- #
# etl
# --------------------------------------------------------------------------- #


def save_etl(analysis: EtlAnalysis, session_id: str | None = None) -> EtlAnalysis:
    with write_session() as s:
        s.add(
            EtlRow(
                id=analysis.id,
                session_id=session_id,
                language=str(analysis.language),
                created_at=analysis.created_at.isoformat(),
                payload=analysis.model_dump(mode="json"),
            )
        )
    return analysis


def get_etl(etl_id: str) -> EtlAnalysis | None:
    with write_session() as s:
        row = s.get(EtlRow, etl_id)
        return EtlAnalysis.model_validate(row.payload) if row else None


# --------------------------------------------------------------------------- #
# plans / runs
# --------------------------------------------------------------------------- #


def save_plan(plan: TestPlan, session_id: str | None = None) -> TestPlan:
    payload = plan.model_dump(mode="json")
    with write_session() as s:
        row = s.get(RunRow, plan.run_id) if plan.run_id else None
        if row is None:
            run_id = plan.run_id or plan.id
            s.add(
                RunRow(
                    id=run_id,
                    session_id=session_id,
                    status="awaiting_approval",
                    created_at=plan.created_at.isoformat(),
                    payload={"plan": payload},
                )
            )
        else:
            row.payload = {**row.payload, "plan": payload}
            row.status = "awaiting_approval"
    plan.run_id = plan.run_id or plan.id
    return plan


def get_plan(run_id: str) -> TestPlan | None:
    with write_session() as s:
        row = s.get(RunRow, run_id)
        if not row or "plan" not in row.payload:
            return None
        return TestPlan.model_validate(row.payload["plan"])


def save_run(result: RunResult, session_id: str | None = None) -> RunResult:
    payload = result.model_dump(mode="json")
    with write_session() as s:
        row = s.get(RunRow, result.id)
        if row is None:
            s.add(
                RunRow(
                    id=result.id,
                    session_id=session_id,
                    status=str(result.status),
                    score=result.score.overall,
                    duration_ms=int(result.duration_ms or 0),
                    created_at=(result.started_at or payload.get("started_at") or utcnow_str()),
                    payload=payload,
                )
            )
        else:
            row.status = str(result.status)
            row.score = result.score.overall
            row.duration_ms = int(result.duration_ms or 0)
            row.payload = payload

        for cr in result.results:
            existing = (
                s.query(CheckResultRow)
                .filter_by(run_id=result.id, check_id=cr.check_id)
                .one_or_none()
            )
            data = cr.model_dump(mode="json")
            if existing is None:
                s.add(
                    CheckResultRow(
                        id=f"{result.id}:{cr.check_id}",
                        run_id=result.id,
                        check_id=cr.check_id,
                        status=str(cr.status),
                        created_at=utcnow_str(),
                        payload=data,
                    )
                )
            else:
                existing.status = str(cr.status)
                existing.payload = data
    return result


def get_run(run_id: str) -> RunResult | None:
    with write_session() as s:
        row = s.get(RunRow, run_id)
        return RunResult.model_validate(row.payload) if row else None


def list_runs(session_id: str | None = None, limit: int = 50) -> list[RunResult]:
    with write_session() as s:
        q = s.query(RunRow)
        if session_id:
            q = q.filter(RunRow.session_id == session_id)
        return [
            RunResult.model_validate(r.payload)
            for r in q.order_by(RunRow.created_at.desc()).limit(limit)
        ]


# --------------------------------------------------------------------------- #
# chat
# --------------------------------------------------------------------------- #


def append_message(session_id: str, message: ChatMessage) -> ChatMessage:
    with write_session() as s:
        s.add(
            ChatMessageRow(
                id=message.id,
                session_id=session_id,
                role=str(message.role),
                created_at=message.created_at.isoformat(),
                payload=message.model_dump(mode="json"),
            )
        )
    return message


def list_messages(session_id: str, limit: int = 200) -> list[ChatMessage]:
    with write_session() as s:
        rows = (
            s.query(ChatMessageRow)
            .filter(ChatMessageRow.session_id == session_id)
            .order_by(ChatMessageRow.created_at)
            .limit(limit)
            .all()
        )
        return [ChatMessage.model_validate(r.payload) for r in rows]