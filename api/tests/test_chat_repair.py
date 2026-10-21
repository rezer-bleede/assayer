"""Free-text connection absorb: repair, then ask before switching."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.routers import chat
from app.routers.chat import EventBuffer
from app.schemas import ConnectionSpec, SourceType
from app.services.connection_repair import AttemptOutcome


def spec(host: str, port: int = 9002, database: str = "property_intel") -> ConnectionSpec:
    return ConnectionSpec(
        type=SourceType.CLICKHOUSE,
        name="warehouse",
        host=host,
        port=port,
        database=database,
        username="default",
        password="local-clickhouse-password",
    )


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "llm_api_key", None)
    monkeypatch.setattr(settings, "llm_base_url", None)
    monkeypatch.setattr(settings, "connection_repair_max_attempts", 6)
    monkeypatch.setattr(settings, "connection_repair_timeout_seconds", 45.0)


def _only_repaired_works(cand: ConnectionSpec) -> AttemptOutcome:
    ok = cand.host == "host.docker.internal" and cand.port == 8123
    return AttemptOutcome(ok=ok, message="" if ok else "Connection refused (Errno 61)")


def test_acceptance_words_are_not_plan_approval() -> None:
    ctx = {"pending_connection": {"spec": {}}}
    assert chat._accepts_pending_connection(ctx, None, "use it")
    assert chat._accepts_pending_connection(ctx, "accept_connection", "")
    # bare "go"/"yes" approve a plan; they must not silently switch connections
    assert not chat._accepts_pending_connection(ctx, None, "go")
    assert not chat._accepts_pending_connection({}, None, "use it")


def test_adopting_a_pending_connection_saves_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat.repo, "save_connection", lambda s, sid: SimpleNamespace(id="conn-9"))
    ctx = {
        "pending_connection": {
            "spec": spec("host.docker.internal", 8123).model_dump(mode="json"),
            "requirement_key": "db:property_intel",
        }
    }
    buf = EventBuffer()

    note = chat._adopt_pending_connection(ctx, "s1", buf)

    assert "conn-9" in ctx["connection_ids"]
    assert ctx["answered"]["db:property_intel"] == "conn-9"
    assert "pending_connection" not in ctx
    assert "host.docker.internal" in (note or "")
    assert [e["type"] for e in buf.drain()] == ["connection.detected"]


async def test_absorb_saves_a_connection_that_just_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        chat, "probe_attempt", lambda cand, tables=None: AttemptOutcome(ok=True, message="ok")
    )
    monkeypatch.setattr(chat.repo, "save_connection", lambda s, sid: SimpleNamespace(id="conn-1"))
    ctx: dict = {}
    buf = EventBuffer()

    note = await chat._absorb_connection(spec("host.docker.internal", 8123), "s1", ctx, buf)

    assert "Captured" in (note or "")
    assert ctx["connection_ids"] == ["conn-1"]
    events = [e["type"] for e in buf.drain()]
    assert events == ["connection.attempt", "connection.detected"]


async def test_absorb_parks_a_repaired_connection_instead_of_saving_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(chat, "probe_attempt", lambda cand, tables=None: _only_repaired_works(cand))
    saved: list[object] = []
    monkeypatch.setattr(
        chat.repo, "save_connection", lambda s, sid: saved.append(s) or SimpleNamespace(id="x")
    )
    ctx: dict = {}
    buf = EventBuffer()

    note = await chat._absorb_connection(spec("localhost", 9002), "s1", ctx, buf)

    assert saved == []  # never saved without the user's say-so
    assert "use it" in (note or "")
    pending = ctx["pending_connection"]
    assert pending["spec"]["host"] == "host.docker.internal"
    assert pending["spec"]["port"] == 8123
    events = [e["type"] for e in buf.drain()]
    assert "connection.attempt" in events
    assert "connection.repair.suggested" in events
    assert "connection.detected" not in events


async def test_absorb_reports_when_nothing_works(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        chat, "probe_attempt", lambda cand, tables=None: AttemptOutcome(ok=False, message="refused")
    )
    monkeypatch.setattr(chat, "list_server_databases", lambda cand: [])
    ctx: dict = {}
    buf = EventBuffer()

    note = await chat._absorb_connection(spec("10.0.0.9", 8123), "s1", ctx, buf)

    assert "couldn't reach" in (note or "")
    assert "pending_connection" not in ctx
    assert "connection.repair.exhausted" in [e["type"] for e in buf.drain()]
