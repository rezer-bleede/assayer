from __future__ import annotations

import contextlib

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.store import repository as repo
from app.store.db import Base, write_session


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """Point the repository at a throwaway SQLite file."""
    import app.store.db as db_mod
    import app.store.repository as repo_mod

    engine = create_engine(
        f"sqlite:///{tmp_path / 'state.db'}", connect_args={"check_same_thread": False}
    )
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(engine)

    @contextlib.contextmanager
    def session():
        s = factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", factory)
    monkeypatch.setattr(repo_mod, "write_session", session)
    yield engine
    engine.dispose()


def _tables(engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def test_saved_connection_is_actually_persisted(store) -> None:
    """Regression: `with SessionLocal()` closes without committing."""
    from app.schemas import ConnectionSpec

    spec = ConnectionSpec(type="postgres", name="dwh", host="db", database="qualitest")
    rec = repo.save_connection(spec, "sess-1")

    assert "connections" in _tables(store)
    assert repo.get_connection(rec.id) is not None
    assert repo.get_connection(rec.id).host == "db"
    assert len(repo.list_connections("sess-1")) == 1


def test_saved_etl_roundtrips(store) -> None:
    from app.schemas import EtlAnalysis

    a = EtlAnalysis(language="sql", sources=["raw.orders"], targets=["analytics.rev"])
    repo.save_etl(a, "sess-1")
    got = repo.get_etl(a.id)
    assert got is not None
    assert got.sources == ["raw.orders"]
    assert got.targets == ["analytics.rev"]


def test_plan_and_run_survive_a_new_session(store) -> None:
    from app.schemas import CheckKind, CheckSpec, RunResult, TableRef, TestPlan

    plan = TestPlan(
        checks=[
            CheckSpec(
                name="pk unique",
                kind=CheckKind.UNIQUE,
                table=TableRef(source_id="c1", schema="analytics", name="rev"),
                column="order_id",
            )
        ]
    )
    plan.run_id = "run-x"
    repo.save_plan(plan, "sess-1")

    # read through a *fresh* session: proves the row hit the database
    assert repo.get_plan("run-x") is not None
    assert repo.get_plan("run-x").checks[0].name == "pk unique"

    run = RunResult(id="run-x", plan=plan, results=[])
    repo.save_run(run, "sess-1")
    assert repo.get_run("run-x") is not None
    assert [r.id for r in repo.list_runs("sess-1")] == ["run-x"]


def test_session_payload_is_saved(store) -> None:
    repo.ensure_session("sess-9")
    repo.update_session_payload("sess-9", {"etl_id": "abc", "pending_plan": None})
    payload = repo.load_session_payload("sess-9")
    assert payload["etl_id"] == "abc"
    assert payload["pending_plan"] is None


def test_chat_messages_are_ordered_and_persisted(store) -> None:
    from app.schemas import ChatMessage, ChatRole

    for i, role in enumerate([ChatRole.USER, ChatRole.ASSISTANT]):
        repo.append_message(
            "sess-2",
            ChatMessage(role=role, content=f"msg {i}"),
        )
    msgs = repo.list_messages("sess-2")
    assert [m.content for m in msgs] == ["msg 0", "msg 1"]


def test_failed_write_rolls_back(store) -> None:
    """A duplicate primary key must not leave a half-written transaction open."""
    from app.schemas import ConnectionSpec
    from app.store.db import ConnectionRow

    spec = ConnectionSpec(type="postgres", name="dwh", host="db")
    first = repo.save_connection(spec, "sess-3")

    # SQLAlchemy wraps the driver's duplicate-key error in IntegrityError
    with pytest.raises(IntegrityError), write_session() as s:
        s.add(
            ConnectionRow(
                id=first.id,
                session_id="sess-3",
                name="dup",
                type="postgres",
                created_at="",
                payload={},
            )
        )

    # store is still usable and the original row is intact
    assert repo.get_connection(first.id) is not None