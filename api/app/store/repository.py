"""High-level state access helpers used by routers and the agent graph."""

from __future__ import annotations

from typing import Any

from app.core.identity import current_user_id, current_workspace_id
from app.schemas import (
    ChatMessage,
    ConnectionRecord,
    ConnectionSpec,
    EtlAnalysis,
    RunResult,
    TestPlan,
    new_id,
)
from app.store.db import (
    ChatMessageRow,
    CheckResultRow,
    ConnectionRow,
    EtlRow,
    ProjectFileRow,
    ProjectRow,
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
            s.add(SessionRow(id=session_id, user_id=current_user_id(), workspace_id=current_workspace_id(), created_at=utcnow_str(), payload={}))
        elif current_user_id() and (exists.user_id != current_user_id() or exists.workspace_id != current_workspace_id()):
            raise PermissionError("thread belongs to another account")


def load_session_payload(session_id: str) -> dict[str, Any]:
    with write_session() as s:
        row = s.get(SessionRow, session_id)
        if row is None or (current_user_id() and (row.user_id != current_user_id() or row.workspace_id != current_workspace_id())):
            return {}
        return dict(row.payload)


def update_session_payload(session_id: str, payload: dict[str, Any]) -> None:
    with write_session() as s:
        row = s.get(SessionRow, session_id)
        if row is None:
            s.add(SessionRow(id=session_id, user_id=current_user_id(), workspace_id=current_workspace_id(), created_at=utcnow_str(), payload=payload))
        elif current_user_id() and (row.user_id != current_user_id() or row.workspace_id != current_workspace_id()):
            raise PermissionError("thread belongs to another account")
        else:
            row.payload = payload


def bind_etl(session_id: str | None, analysis: EtlAnalysis) -> None:
    """Attach an analysed codebase to the conversation without chat metadata."""
    if not session_id:
        return
    ensure_session(session_id)
    ctx = load_session_payload(session_id)
    ctx["etl_id"] = analysis.id
    ctx["requirements"] = [r.model_dump(mode="json") for r in analysis.required_sources]
    ctx["stage"] = "need_sources" if analysis.required_sources else "ready"
    ctx["question"] = None
    ctx["missing_tables"] = []
    ctx["stage_updated_at"] = utcnow_str()
    update_session_payload(session_id, ctx)


def bind_connection(
    session_id: str | None,
    connection_id: str | None,
    requirement_key: str | None,
) -> None:
    """Attach a REST-verified connection to the conversation."""
    if not (session_id and connection_id):
        return
    ensure_session(session_id)
    ctx = load_session_payload(session_id)
    ids = list(ctx.get("connection_ids", []))
    if connection_id not in ids:
        ids.append(connection_id)
    ctx["connection_ids"] = ids
    if requirement_key:
        ctx.setdefault("answered", {})[requirement_key] = connection_id
    ctx["question"] = None
    ctx["stage"] = "need_sources"
    ctx["stage_updated_at"] = utcnow_str()
    update_session_payload(session_id, ctx)


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
                user_id=current_user_id(),
                workspace_id=current_workspace_id(),
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
        if row is not None and current_user_id() and (row.user_id != current_user_id() or row.workspace_id != current_workspace_id()):
            return None
        return ConnectionSpec.model_validate(row.payload) if row else None


def list_connections(session_id: str | None = None) -> list[ConnectionSpec]:
    with write_session() as s:
        q = s.query(ConnectionRow)
        if session_id:
            q = q.filter(ConnectionRow.session_id == session_id)
        if current_user_id():
            q = q.filter(ConnectionRow.user_id == current_user_id(), ConnectionRow.workspace_id == current_workspace_id())
        return [
            ConnectionSpec.model_validate(r.payload) for r in q.order_by(ConnectionRow.created_at)
        ]


# --------------------------------------------------------------------------- #
# etl
# --------------------------------------------------------------------------- #


def save_etl(analysis: EtlAnalysis, session_id: str | None = None) -> EtlAnalysis:
    with write_session() as s:
        s.add(
            EtlRow(
                id=analysis.id,
                session_id=session_id,
                user_id=current_user_id(),
                workspace_id=current_workspace_id(),
                language=str(analysis.language),
                created_at=analysis.created_at.isoformat(),
                payload=analysis.model_dump(mode="json"),
            )
        )
    return analysis


def get_etl(etl_id: str) -> EtlAnalysis | None:
    with write_session() as s:
        row = s.get(EtlRow, etl_id)
        if row is not None and current_user_id() and (row.user_id != current_user_id() or row.workspace_id != current_workspace_id()):
            return None
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
                    user_id=current_user_id(),
                    workspace_id=current_workspace_id(),
                    status="awaiting_approval",
                    created_at=plan.created_at.isoformat(),
                    payload={"plan": payload},
                )
            )
        else:
            if current_user_id() and (row.user_id != current_user_id() or row.workspace_id != current_workspace_id()):
                raise PermissionError("run belongs to another account or workspace")
            row.payload = {**row.payload, "plan": payload}
            row.status = "awaiting_approval"
    plan.run_id = plan.run_id or plan.id
    return plan


def get_plan(run_id: str) -> TestPlan | None:
    with write_session() as s:
        row = s.get(RunRow, run_id)
        if not row or (current_user_id() and (row.user_id != current_user_id() or row.workspace_id != current_workspace_id())) or "plan" not in row.payload:
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
                    user_id=current_user_id(),
                    workspace_id=current_workspace_id(),
                    status=str(result.status),
                    score=result.score.overall,
                    duration_ms=int(result.duration_ms or 0),
                    created_at=(result.started_at or payload.get("started_at") or utcnow_str()),
                    payload=payload,
                )
            )
        else:
            if current_user_id() and (row.user_id != current_user_id() or row.workspace_id != current_workspace_id()):
                raise PermissionError("run belongs to another account or workspace")
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
        if row is None or (current_user_id() and (row.user_id != current_user_id() or row.workspace_id != current_workspace_id())):
            return None
        return RunResult.model_validate(row.payload)


def list_runs(session_id: str | None = None, limit: int = 50) -> list[RunResult]:
    with write_session() as s:
        q = s.query(RunRow)
        if current_user_id():
            q = q.filter(RunRow.user_id == current_user_id(), RunRow.workspace_id == current_workspace_id())
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
    ensure_session(session_id)
    payload = message.model_dump(mode="json")
    with write_session() as s:
        row = s.get(ChatMessageRow, message.id)
        if row is None:
            s.add(
                ChatMessageRow(
                    id=message.id,
                    session_id=session_id,
                    role=str(message.role),
                    created_at=message.created_at.isoformat(),
                    payload=payload,
                )
            )
        else:
            row.payload = payload
            row.role = str(message.role)
    return message


def get_message(message_id: str) -> ChatMessage | None:
    with write_session() as s:
        row = s.get(ChatMessageRow, message_id)
        return ChatMessage.model_validate(row.payload) if row else None


def list_threads() -> list[dict[str, Any]]:
    with write_session() as s:
        q = s.query(SessionRow)
        uid = current_user_id()
        if uid:
            q = q.filter(SessionRow.workspace_id == current_workspace_id(), SessionRow.user_id == uid)
        rows = q.order_by(SessionRow.created_at.desc()).all()
        return [
            {
                "id": r.id,
                "title": (r.payload or {}).get("title") or "New audit",
                "createdAt": r.created_at,
                "projectId": (r.payload or {}).get("project_id"),
            }
            for r in rows
        ]


def rename_thread(session_id: str, title: str) -> dict[str, Any] | None:
    with write_session() as s:
        row = s.get(SessionRow, session_id)
        if row is None or (current_user_id() and (row.user_id != current_user_id() or row.workspace_id != current_workspace_id())):
            return None
        payload = dict(row.payload or {})
        payload["title"] = title[:120]
        row.payload = payload
        return {"id": row.id, "title": payload["title"], "createdAt": row.created_at}


def delete_thread(session_id: str) -> None:
    with write_session() as s:
        thread = s.get(SessionRow, session_id)
        if thread is None or (current_user_id() and (thread.user_id != current_user_id() or thread.workspace_id != current_workspace_id())):
            return
        for row in s.query(ChatMessageRow).filter(ChatMessageRow.session_id == session_id).all():
            s.delete(row)
        row = s.get(SessionRow, session_id)
        if row is not None:
            s.delete(row)


def list_messages(session_id: str, limit: int = 200) -> list[ChatMessage]:
    with write_session() as s:
        thread = s.get(SessionRow, session_id)
        if thread is None or (current_user_id() and (thread.user_id != current_user_id() or thread.workspace_id != current_workspace_id())):
            return []
        rows = (
            s.query(ChatMessageRow)
            .filter(ChatMessageRow.session_id == session_id)
            .order_by(ChatMessageRow.created_at)
            .limit(limit)
            .all()
        )
        return [ChatMessage.model_validate(r.payload) for r in rows]


# --------------------------------------------------------------------------- #
# projects (ChatGPT/Claude-style grouping: instructions + knowledge files)
# --------------------------------------------------------------------------- #


def save_project(
    name: str, instructions: str = "", project_id: str | None = None
) -> dict[str, Any]:
    pid = project_id or new_id()
    with write_session() as s:
        s.add(
            ProjectRow(
                id=pid,
                name=name[:120],
                instructions=instructions[:8000],
                created_at=utcnow_str(),
                owner_id=current_user_id(),
                workspace_id=current_workspace_id(),
                payload={},
            )
        )
    return get_project(pid) or {"id": pid, "name": name, "instructions": instructions}


def list_projects() -> list[dict[str, Any]]:
    with write_session() as s:
        q = s.query(ProjectRow)
        if current_user_id():
            q = q.filter(ProjectRow.workspace_id == current_workspace_id())
        projects = q.order_by(ProjectRow.created_at.desc()).all()
        counts: dict[str, int] = {}
        file_counts: dict[str, int] = {}
        session_query = s.query(SessionRow)
        if current_user_id():
            session_query = session_query.filter(SessionRow.user_id == current_user_id(), SessionRow.workspace_id == current_workspace_id())
        for row in session_query.all():
            pid = (row.payload or {}).get("project_id")
            if pid:
                counts[pid] = counts.get(pid, 0) + 1
        file_query = s.query(ProjectFileRow)
        if current_user_id():
            file_query = file_query.filter(ProjectFileRow.workspace_id == current_workspace_id())
        for f in file_query.all():
            file_counts[f.project_id] = file_counts.get(f.project_id, 0) + 1
        return [
            {
                "id": p.id,
                "name": p.name,
                "instructions": p.instructions,
                "createdAt": p.created_at,
                "threadCount": counts.get(p.id, 0),
                "fileCount": file_counts.get(p.id, 0),
            }
            for p in projects
        ]


def get_project(project_id: str) -> dict[str, Any] | None:
    with write_session() as s:
        p = s.get(ProjectRow, project_id)
        if p is None or (current_user_id() and p.workspace_id != current_workspace_id()):
            return None
        return {
            "id": p.id,
            "name": p.name,
            "instructions": p.instructions,
            "createdAt": p.created_at,
        }


def update_project(
    project_id: str, name: str | None = None, instructions: str | None = None
) -> dict[str, Any] | None:
    with write_session() as s:
        p = s.get(ProjectRow, project_id)
        if p is None or (current_user_id() and p.workspace_id != current_workspace_id()):
            return None
        if name is not None and name.strip():
            p.name = name.strip()[:120]
        if instructions is not None:
            p.instructions = instructions[:8000]
    return get_project(project_id)


def delete_project(project_id: str) -> bool:
    """Delete the project; its chats revert to unfiled, its files go with it."""
    with write_session() as s:
        p = s.get(ProjectRow, project_id)
        if p is None or (current_user_id() and p.workspace_id != current_workspace_id()):
            return False
        for f in s.query(ProjectFileRow).filter(ProjectFileRow.project_id == project_id).all():
            s.delete(f)
        session_query = s.query(SessionRow)
        if current_user_id():
            session_query = session_query.filter(SessionRow.user_id == current_user_id(), SessionRow.workspace_id == current_workspace_id())
        for row in session_query.all():
            payload = dict(row.payload or {})
            if payload.get("project_id") == project_id:
                payload.pop("project_id", None)
                row.payload = payload
        s.delete(p)
        return True


def add_project_file(project_id: str, name: str, text: str) -> dict[str, Any]:
    fid = new_id()
    with write_session() as s:
        s.add(
            ProjectFileRow(
                id=fid,
                project_id=project_id,
                name=name[:255],
                size=len(text.encode("utf-8", "replace")),
                created_at=utcnow_str(),
                owner_id=current_user_id(),
                workspace_id=current_workspace_id(),
                payload={"text": text},
            )
        )
    return {"id": fid, "name": name, "size": len(text.encode("utf-8", "replace"))}


def list_project_files(project_id: str, with_content: bool = False) -> list[dict[str, Any]]:
    with write_session() as s:
        rows = (
            s.query(ProjectFileRow)
            .filter(ProjectFileRow.project_id == project_id)
            .filter(ProjectFileRow.workspace_id == current_workspace_id() if current_user_id() else True)
            .order_by(ProjectFileRow.created_at)
            .all()
        )
        out = []
        for r in rows:
            item: dict[str, Any] = {
                "id": r.id,
                "name": r.name,
                "size": r.size,
                "createdAt": r.created_at,
            }
            if with_content:
                item["text"] = (r.payload or {}).get("text", "")
            out.append(item)
        return out


def delete_project_file(project_id: str, file_id: str) -> bool:
    with write_session() as s:
        row = s.get(ProjectFileRow, file_id)
        if row is None or row.project_id != project_id or (current_user_id() and row.workspace_id != current_workspace_id()):
            return False
        s.delete(row)
        return True


def get_thread_project_id(session_id: str) -> str | None:
    ctx = load_session_payload(session_id)
    pid = ctx.get("project_id")
    return str(pid) if pid else None


def set_thread_project(session_id: str, project_id: str | None) -> dict[str, Any] | None:
    ensure_session(session_id)
    ctx = load_session_payload(session_id)
    if project_id:
        ctx["project_id"] = project_id
    else:
        ctx.pop("project_id", None)
    update_session_payload(session_id, ctx)
    return {"id": session_id, "projectId": project_id}
