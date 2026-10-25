"""SQLAlchemy engine/session for app state (connections, ETL, plans, runs, chat, projects)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import JSON, DateTime, Float, Integer, String, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from app.core.config import settings
from app.core.logging import log


class Base(DeclarativeBase):
    pass


class SessionRow(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[str] = mapped_column(String(40), default="")
    user_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    workspace_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class ConnectionRow(Base):
    __tablename__ = "connections"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    user_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    workspace_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    name: Mapped[str] = mapped_column(String(128))
    type: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[str] = mapped_column(String(40), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class EtlRow(Base):
    __tablename__ = "etl_analyses"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    user_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    workspace_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    language: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[str] = mapped_column(String(40), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class RunRow(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    user_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    workspace_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default="pending")
    score: Mapped[float] = mapped_column(Float, default=0.0)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class CheckResultRow(Base):
    __tablename__ = "check_results"

    # composite key f"{run_id}:{check_id}" is 32+1+32 = 65 chars
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    check_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[str] = mapped_column(String(40), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class ChatMessageRow(Base):
    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[str] = mapped_column(String(40), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class ProjectRow(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    instructions: Mapped[str] = mapped_column(String(8000), default="")
    created_at: Mapped[str] = mapped_column(String(40), default="")
    owner_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    workspace_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class ProjectFileRow(Base):
    __tablename__ = "project_files"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(40), default="")
    owner_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    workspace_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(256), default="")
    role: Mapped[str] = mapped_column(String(32), default="member")
    created_at: Mapped[str] = mapped_column(String(40), default="")
    settings: Mapped[dict] = mapped_column(JSON, default=dict)


class LoginSessionRow(Base):
    __tablename__ = "login_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[str] = mapped_column(String(40), index=True)
    created_at: Mapped[str] = mapped_column(String(40), default="")


class GuestSessionRow(Base):
    __tablename__ = "guest_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[str] = mapped_column(String(40), index=True)
    created_at: Mapped[str] = mapped_column(String(40), default="")


class WorkspaceRow(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    owner_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="personal")
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(40), default="")


class WorkspaceMemberRow(Base):
    __tablename__ = "workspace_members"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(32), default="member")
    created_at: Mapped[str] = mapped_column(String(40), default="")


class AppSettingsRow(Base):
    __tablename__ = "app_settings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    values: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[str] = mapped_column(String(40), default="")


class WorkspaceInviteRow(Base):
    __tablename__ = "workspace_invites"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(32), default="member")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_by: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[str] = mapped_column(String(40), index=True)
    accepted_at: Mapped[str | None] = mapped_column(String(40), nullable=True, default=None)
    created_at: Mapped[str] = mapped_column(String(40), default="")


class IntegrationRow(Base):
    """Encrypted provider credentials owned by one user in one workspace."""

    __tablename__ = "integrations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)
    account_id: Mapped[str] = mapped_column(String(128), default="")
    account_name: Mapped[str] = mapped_column(String(255), default="")
    encrypted_credentials: Mapped[str] = mapped_column(String(8192))
    created_at: Mapped[str] = mapped_column(String(40), default="")
    updated_at: Mapped[str] = mapped_column(String(40), default="")


class ProjectIntegrationRow(Base):
    """Provider resource explicitly enabled for a project."""

    __tablename__ = "project_integrations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    integration_id: Mapped[str] = mapped_column(String(64), index=True)
    resource_id: Mapped[str] = mapped_column(String(512))
    resource_name: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[str] = mapped_column(String(40), default="")


class IntegrationActionRow(Base):
    """Preview-first external write awaiting explicit user confirmation."""

    __tablename__ = "integration_actions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    integration_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(40), default="")
    updated_at: Mapped[str] = mapped_column(String(40), default="")


def _engine_kwargs(url: str) -> dict:
    kwargs: dict = {"pool_pre_ping": True, "future": True}
    if url.startswith("postgresql"):
        kwargs.update({"pool_size": 10, "max_overflow": 20})
    elif url.startswith("sqlite"):
        kwargs.pop("pool_pre_ping", None)
        kwargs["connect_args"] = {"check_same_thread": False}
    return kwargs


engine = create_engine(settings.database_url, **_engine_kwargs(settings.database_url))
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator:
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


@contextmanager
def write_session() -> Iterator:
    """Unit-of-work session: commit on success, roll back on error, always close.

    `SessionLocal()` used as a bare context manager *closes* without committing,
    which silently discards every write — always use this for persistence.
    """
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_db() -> Iterator:
    with session_scope() as s:
        yield s


def init_db() -> None:
    """Create tables. Safe on every boot."""
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
    except Exception as exc:  # sqlite / restricted users
        log.debug("pgcrypto extension skipped", error=str(exc))

    Base.metadata.create_all(engine)
    _ensure_auth_columns(engine)
    _widen_composite_pk(engine)
    log.info("state store ready")


def _ensure_auth_columns(eng) -> None:
    """Add nullable ownership columns to databases created before accounts."""
    statements = (
        "ALTER TABLE sessions ADD COLUMN user_id VARCHAR(64)",
        "ALTER TABLE sessions ADD COLUMN workspace_id VARCHAR(64)",
        "ALTER TABLE connections ADD COLUMN user_id VARCHAR(64)",
        "ALTER TABLE connections ADD COLUMN workspace_id VARCHAR(64)",
        "ALTER TABLE etl_analyses ADD COLUMN user_id VARCHAR(64)",
        "ALTER TABLE etl_analyses ADD COLUMN workspace_id VARCHAR(64)",
        "ALTER TABLE runs ADD COLUMN user_id VARCHAR(64)",
        "ALTER TABLE runs ADD COLUMN workspace_id VARCHAR(64)",
        "ALTER TABLE projects ADD COLUMN owner_id VARCHAR(64)",
        "ALTER TABLE projects ADD COLUMN workspace_id VARCHAR(64)",
        "ALTER TABLE project_files ADD COLUMN owner_id VARCHAR(64)",
        "ALTER TABLE project_files ADD COLUMN workspace_id VARCHAR(64)",
    )
    for statement in statements:
        try:
            with eng.begin() as conn:
                conn.execute(text(statement))
        except Exception:  # already present (or unsupported dialect)
            pass


def _widen_composite_pk(eng) -> None:
    """Idempotently widen the composite check-results key.

    `create_all` never alters an existing table, so databases created before the
    key grew past 64 chars would keep failing on insert.
    """
    try:
        with eng.begin() as conn:
            conn.execute(text("ALTER TABLE check_results ALTER COLUMN id TYPE VARCHAR(128)"))
    except Exception as exc:  # noqa: BLE001 - column already wide, or table absent
        log.debug("check_results id width unchanged", error=str(exc))


def utcnow_str() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


__all__ = [
    "Base",
    "SessionRow",
    "ConnectionRow",
    "EtlRow",
    "RunRow",
    "CheckResultRow",
    "ChatMessageRow",
    "ProjectRow",
    "ProjectFileRow",
    "IntegrationRow",
    "ProjectIntegrationRow",
    "IntegrationActionRow",
    "engine",
    "SessionLocal",
    "session_scope",
    "get_db",
    "init_db",
    "write_session",
    "utcnow_str",
    "DateTime",
]
