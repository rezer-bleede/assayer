"""SQLAlchemy engine/session for app state (connections, ETL, plans, runs, chat)."""

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
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class ConnectionRow(Base):
    __tablename__ = "connections"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    name: Mapped[str] = mapped_column(String(128))
    type: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[str] = mapped_column(String(40), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class EtlRow(Base):
    __tablename__ = "etl_analyses"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    language: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[str] = mapped_column(String(40), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class RunRow(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
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
    _widen_composite_pk(engine)
    log.info("state store ready")


def _widen_composite_pk(eng) -> None:
    """Idempotently widen the composite check-results key.

    `create_all` never alters an existing table, so databases created before the
    key grew past 64 chars would keep failing on insert.
    """
    try:
        with eng.begin() as conn:
            conn.execute(
                text("ALTER TABLE check_results ALTER COLUMN id TYPE VARCHAR(128)")
            )
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
    "engine",
    "SessionLocal",
    "session_scope",
    "get_db",
    "init_db",
    "write_session",
    "utcnow_str",
    "DateTime",
]