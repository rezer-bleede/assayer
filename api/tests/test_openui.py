from __future__ import annotations

import asyncio
import contextlib
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agent.openui import (
    notice_program,
    plan_program,
    program_for_state,
    question_program,
    report_program,
    welcome_program,
)
from app.agent.openui_validate import clean_program, validate_program
from app.routers import agent as agent_router
from app.routers import threads as threads_router
from app.schemas import (
    CheckKind,
    CheckResult,
    CheckSpec,
    CheckStatus,
    ConversationStage,
    EtlAnalysis,
    EtlLanguage,
    EtlOrigin,
    QualityScore,
    Question,
    QuestionKind,
    QuestionOption,
    RunResult,
    Severity,
    SourceRequirement,
    TableRef,
    TestPlan,
)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    import app.store.db as db_mod
    import app.store.repository as repo_mod
    from app.store.db import Base

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


def _plan() -> TestPlan:
    return TestPlan(
        id="run-1",
        run_id="run-1",
        summary="Nulls and uniqueness.",
        checks=[
            CheckSpec(
                name="No null order ids",
                kind=CheckKind.NULL_RATE,
                table=TableRef(source_id="c1", schema="public", name="orders"),
                column="id",
                severity=Severity.CRITICAL,
                rationale="Order id is the grain.",
            )
        ],
    )


def _report() -> RunResult:
    return RunResult(
        id="run-1",
        score=QualityScore(overall=92.5, passed=1, failed=0, errored=0, skipped=0),
        duration_ms=1200,
        results=[
            CheckResult(
                check_id="c1",
                name="No null order ids",
                kind=CheckKind.NULL_RATE,
                table=TableRef(source_id="c1", schema="public", name="orders"),
                column="id",
                status=CheckStatus.PASSED,
                observed="0%",
                expected="0%",
                message="No nulls.",
            )
        ],
    )


def test_validator_strips_fences_and_prose() -> None:
    raw = 'Here is the UI:\n```openui-lang\nroot = Card([Notice("ok")])\n```\nDone.'
    program, error = validate_program(raw)
    assert error is None
    assert program == 'root = Card([Notice("ok")])'
    assert clean_program(raw) == 'root = Card([Notice("ok")])'


def test_validator_rejects_unknown_component_and_non_card_root() -> None:
    assert validate_program('root = Card([MadeUp("x")])')[1] == "unknown component MadeUp"
    assert validate_program('root = Notice("x")')[1] == "root must be Card, got Notice"


def test_validator_rejects_unbalanced_program() -> None:
    assert validate_program('root = Card([Notice("x")]') == (None, "unbalanced brackets or quotes")


def test_fallback_programs_are_valid() -> None:
    q = Question(
        stage=ConversationStage.NEED_SOURCES,
        title="Where does orders live?",
        prompt="The ETL reads orders.",
        requirement_key="req-1",
        options=[
            QuestionOption(
                kind=QuestionKind.CONNECTION,
                label="Connection URL",
                help="Stored as a secret.",
                placeholder="postgresql://user:pass@host:5432/db",
            )
        ],
    )
    analysis = EtlAnalysis(
        language=EtlLanguage.SQL,
        origin=EtlOrigin.GIT,
        label="analytics",
        files=["models/orders.sql"],
        sources=["raw.orders"],
        targets=["marts.orders"],
        required_sources=[SourceRequirement(key="req-1", tables=["raw.orders"])],
    )
    programs = [
        welcome_program("Hi", "Body", ["Audit a dbt project"]),
        notice_program("Body", title="Status", tone="success"),
        question_program(q, analysis),
        plan_program(_plan(), ["raw.missing"]),
        report_program(_report()),
    ]
    for program in programs:
        _clean, error = validate_program(
            program if program.startswith("root") else f"root = Card([{program}])"
        )
        assert error is None, (program, error)


def test_program_for_state_wraps_question_and_etl_in_one_card() -> None:
    q = Question(
        stage=ConversationStage.NEED_SOURCES,
        title="Where does orders live?",
        prompt="The ETL reads orders.",
        requirement_key="req-1",
        options=[QuestionOption(kind=QuestionKind.CONNECTION, label="Connection URL")],
    )
    analysis = EtlAnalysis(
        language=EtlLanguage.SQL,
        origin=EtlOrigin.GIT,
        label="analytics",
        files=["models/orders.sql"],
        sources=["raw.orders"],
        targets=["marts.orders"],
        required_sources=[SourceRequirement(key="req-1", tables=["raw.orders"])],
    )
    program = program_for_state(ctx={}, question=q, plan=None, run=None, analysis=analysis)
    assert program.startswith("root = Card([EtlCard(")
    assert "IntakeCard(" in program
    assert validate_program(program)[1] is None


def test_clean_user_text_unwraps_openui_action_envelopes() -> None:
    from app.routers.agent import clean_user_text

    raw = (
        "]]>openui:content\nThe codebase is ready.\n"
        ']]>openui:context\n["User clicked: The codebase is ready.",{}]'
    )
    assert clean_user_text(raw) == "The codebase is ready."
    assert clean_user_text('\n]]>openui:context\n["User clicked: skip",{}]') == "skip"
    assert clean_user_text("plain hello") == "plain hello"


def test_upload_binds_analysis_to_the_session(store) -> None:
    from app.routers import etl as etl_router
    from app.store import repository as repo

    class Upload:
        def __init__(self, filename: str, body: bytes) -> None:
            self.filename = filename
            self._body = body
            self.size = len(body)

        async def read(self) -> bytes:
            return self._body

    result = asyncio.run(
        etl_router.upload(
            session_id="thread-1",
            mode="folder",
            files=[
                Upload("dbt_project.yml", b"name: shop\n"),
                Upload("stg_orders.sql", b"select * from {{ source('raw', 'orders') }}"),
            ],
            paths=json.dumps(["dbt_project.yml", "models/stg_orders.sql"]),
        )
    )
    assert result.analysis.required_sources
    payload = repo.load_session_payload("thread-1")
    assert payload["etl_id"] == result.analysis.id
    assert payload["stage"] == "need_sources"


def test_program_for_state_renders_a_pending_repair() -> None:
    program = program_for_state(
        ctx={
            "pending_connection": {
                "spec": {
                    "type": "postgres",
                    "name": "warehouse",
                    "host": "db",
                    "port": 5432,
                    "database": "analytics",
                }
            }
        },
        question=None,
        plan=None,
        run=None,
        analysis=None,
    )
    assert program.startswith("root = Card([RepairCard(")
    assert '"db", 5432' in program
    assert validate_program(program)[1] is None


def test_thread_endpoints_roundtrip(store) -> None:
    created = threads_router.create_thread(
        threads_router.CreateThreadRequest(
            messages=[{"id": "m1", "role": "user", "content": "Audit my dbt project"}]
        )
    )
    assert created["title"] == "Audit my dbt project"

    listing = threads_router.list_threads()
    assert listing["threads"][0]["id"] == created["id"]

    messages = threads_router.get_thread_messages(created["id"])
    assert messages[0]["content"] == "Audit my dbt project"

    renamed = threads_router.update_thread(
        created["id"], threads_router.UpdateThreadRequest(title="Renamed")
    )
    assert renamed["title"] == "Renamed"

    threads_router.delete_thread(created["id"])
    assert threads_router.list_threads()["threads"] == []


async def test_agent_stream_emits_one_openui_text_message(store, monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "openui_author_enabled", False)
    created = threads_router.create_thread(
        threads_router.CreateThreadRequest(messages=[{"role": "user", "content": "hello"}])
    )
    response = await agent_router.agent(
        agent_router.AgentRequest(
            threadId=created["id"],
            messages=[{"id": "m1", "role": "user", "content": "hello"}],
        ),
        None,  # type: ignore[arg-type]
    )
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    frames = [
        json.loads(line[6:])
        for chunk in chunks
        for line in chunk.splitlines()
        if line.startswith("data: ")
    ]
    types = [f["type"] for f in frames]
    assert types == ["TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END"]
    assert frames[1]["delta"].startswith("root = Card(")
    assert validate_program(frames[1]["delta"])[1] is None

    persisted = threads_router.get_thread_messages(created["id"])
    assert persisted[-1]["role"] == "assistant"
    assert persisted[-1]["content"].startswith("root = Card(")
