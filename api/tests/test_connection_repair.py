"""The connection-repair loop: ladder first, model second, always bounded."""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config import settings
from app.schemas import ConnectionSpec, SourceType
from app.services import connection_repair as cr


def ch(host: str, port: int | None = 9002, database: str = "property_intel") -> ConnectionSpec:
    return ConnectionSpec(
        type=SourceType.CLICKHOUSE,
        name="warehouse",
        host=host,
        port=port,
        database=database,
        username="default",
        password="local-clickhouse-password",
    )


def just_the_user_case(cand: ConnectionSpec) -> cr.AttemptOutcome:
    """Only host.docker.internal:8123 works — the bug that started all this."""
    ok = cand.host == "host.docker.internal" and cand.port == 8123
    return cr.AttemptOutcome(ok=ok, message="" if ok else "Connection refused (Errno 61)")


async def no_llm() -> bool:
    return False


@pytest.fixture(autouse=True)
def _defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "connection_repair_enabled", True)
    monkeypatch.setattr(settings, "connection_repair_max_attempts", 6)
    monkeypatch.setattr(settings, "connection_repair_max_llm_rounds", 3)
    monkeypatch.setattr(settings, "connection_repair_timeout_seconds", 45.0)
    # no LLM by default, so no test ever reaches out to a real model; the tests
    # that exercise the model turn it back on explicitly
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "llm_api_key", None)
    monkeypatch.setattr(settings, "llm_base_url", None)


async def test_deterministic_ladder_finds_the_host_and_port_fix() -> None:
    """Both a host *and* a port fix are needed, so the grid must cross them."""
    result = await cr.repair_connection(ch("localhost"), attempt=just_the_user_case)

    assert result.ok and result.repaired
    assert result.suggested is not None
    assert (result.suggested.host, result.suggested.port) == ("host.docker.internal", 8123)
    assert result.attempts[0].source == "original"
    assert result.attempts[0].outcome == "failed"
    assert [a.n for a in result.attempts] == list(range(1, len(result.attempts) + 1))


async def test_candidates_change_the_fewest_fields_first() -> None:
    """Cheap single-field fixes are tried before combinations of them."""
    tried: list[tuple[str | None, int | None]] = []

    def attempt(cand: ConnectionSpec) -> cr.AttemptOutcome:
        tried.append((cand.host, cand.port))
        return cr.AttemptOutcome(ok=False, message="refused")

    await cr.repair_connection(ch("localhost", 9002), attempt=attempt)
    changed = [int(h != "localhost") + int(p != 9002) for h, p in tried]
    assert changed == sorted(changed)
    # and the combination that actually fixes the user's case is reachable
    assert ("host.docker.internal", 8123) in tried


async def test_first_try_success_needs_no_repair() -> None:
    result = await cr.repair_connection(
        ch("host.docker.internal", 8123), attempt=just_the_user_case
    )
    assert result.ok and not result.repaired
    assert result.suggested is None
    assert len(result.attempts) == 1
    assert "first try" in (result.note or "")


async def test_attempts_are_deduplicated() -> None:
    seen: list[cr.ConnectionSpec] = []

    def attempt(cand: ConnectionSpec) -> cr.AttemptOutcome:
        seen.append(cand)
        return cr.AttemptOutcome(ok=False, message="Connection refused")

    await cr.repair_connection(
        ch("localhost", 9002), attempt=attempt, hints={"port": 8123}
    )
    keys = [(c.host, c.port, c.database) for c in seen]
    assert len(keys) == len(set(keys))


async def test_attempts_are_capped_and_disclosed() -> None:
    result = await cr.repair_connection(
        ch("10.0.0.9", 8123), attempt=lambda c: cr.AttemptOutcome(ok=False, message="timed out")
    )
    assert result.exhausted and not result.ok
    assert len(result.attempts) <= settings.connection_repair_max_attempts


async def test_missing_database_is_answered_with_the_real_one() -> None:
    def attempt(cand: ConnectionSpec) -> cr.AttemptOutcome:
        if cand.database == "property_intel":
            return cr.AttemptOutcome(ok=True, message="connected")
        return cr.AttemptOutcome(ok=False, message="Database dubai_data_third_party does not exist")

    result = await cr.repair_connection(
        ch("host.docker.internal", 8123, "dubai_data_third_party"),
        attempt=attempt,
        list_databases=lambda spec: ["property_intel", "system"],
    )
    assert result.repaired
    assert result.suggested is not None and result.suggested.database == "property_intel"


async def test_enumeration_uses_the_address_that_reached_the_server() -> None:
    """The broken original address must not be reused to list databases."""
    enumerated_from: list[str | None] = []

    def attempt(cand: ConnectionSpec) -> cr.AttemptOutcome:
        if cand.host == "localhost":
            return cr.AttemptOutcome(ok=False, message="Connection refused")
        if cand.database == "property_intel":
            return cr.AttemptOutcome(ok=True, message="connected")
        return cr.AttemptOutcome(
            ok=False, message="Database dubai_data_third_party does not exist"
        )

    def list_dbs(cand: ConnectionSpec) -> list[str]:
        enumerated_from.append(cand.host)
        return ["property_intel"]

    result = await cr.repair_connection(
        ch("localhost", 9002, "dubai_data_third_party"),
        attempt=attempt,
        list_databases=list_dbs,
    )

    assert enumerated_from == ["host.docker.internal"]
    assert result.repaired and result.suggested is not None
    assert (result.suggested.host, result.suggested.port) == ("host.docker.internal", 8123)
    assert result.suggested.database == "property_intel"


async def test_llm_round_runs_only_after_the_ladder_and_never_sees_secrets(    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    async def fake_ainvoke(prompt: Any, **_: Any) -> str:
        captured["prompt"] = str(prompt)
        return '{"candidates": [{"host": "db.internal", "port": 8123, "reason": "vpn name"}]}'

    monkeypatch.setattr(settings, "llm_provider", "ollama")
    monkeypatch.setattr(settings, "llm_base_url", "http://ollama:11434")
    monkeypatch.setattr("app.core.llm.ainvoke", fake_ainvoke)

    def attempt(cand: ConnectionSpec) -> cr.AttemptOutcome:
        return cr.AttemptOutcome(ok=cand.host == "db.internal", message="no route to host")

    result = await cr.repair_connection(ch("10.0.0.9"), attempt=attempt)

    assert result.repaired and result.suggested is not None
    assert result.suggested.host == "db.internal"
    assert result.attempts[-1].source == "llm"
    assert "local-clickhouse-password" not in captured["prompt"]


async def test_llm_garbage_is_ignored_not_trusted(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_ainvoke(_prompt: Any, **_: Any) -> str:
        return "not json at all, sorry"

    monkeypatch.setattr(settings, "llm_provider", "ollama")
    monkeypatch.setattr(settings, "llm_base_url", "http://ollama:11434")
    monkeypatch.setattr("app.core.llm.ainvoke", fake_ainvoke)

    result = await cr.repair_connection(
        ch("10.0.0.9", 8123), attempt=lambda c: cr.AttemptOutcome(ok=False, message="nope")
    )
    assert result.exhausted
    assert all(a.source != "llm" for a in result.attempts)


def test_candidates_may_only_change_host_port_database() -> None:
    spec = ch("localhost", 9002)
    patched = cr._apply_llm_candidate(
        spec,
        {
            "host": "db.internal",
            "port": 8443,
            "database": "analytics",
            "username": "hacker",
            "password": "hunter2",
            "type": "postgres",
        },
    )
    assert patched is not None
    assert (patched.host, patched.port, patched.database) == ("db.internal", 8443, "analytics")
    assert patched.username == spec.username
    assert patched.password.get_secret_value() == "local-clickhouse-password"
    assert patched.type == SourceType.CLICKHOUSE


def test_invalid_candidates_are_rejected() -> None:
    spec = ch("localhost", 9002)
    assert cr._apply_llm_candidate(spec, {"host": "bad host", "port": 99999}) is None
    assert cr._apply_llm_candidate(spec, {}) is None


async def test_progress_is_streamed_as_it_happens() -> None:
    events: list[tuple[str, dict]] = []
    await cr.repair_connection(
        ch("localhost"),
        attempt=just_the_user_case,
        emit=lambda name, data: events.append((name, data)),
    )
    assert events
    assert all(name == "connection.attempt" for name, _ in events)
    assert events[0][1]["outcome"] == "failed"
    assert events[-1][1]["outcome"] == "ok"
    assert events[-1][1]["host"] == "host.docker.internal"


async def test_disabled_repair_still_probes_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "connection_repair_enabled", False)
    calls = 0

    def attempt(cand: ConnectionSpec) -> cr.AttemptOutcome:
        nonlocal calls
        calls += 1
        return cr.AttemptOutcome(ok=False, message="refused")

    result = await cr.repair_connection(ch("localhost"), attempt=attempt)
    assert calls == 1
    assert result.exhausted and "disabled" in (result.note or "")


async def test_report_redacts_the_suggestion() -> None:
    result = await cr.repair_connection(ch("localhost"), attempt=just_the_user_case)
    report = result.report()
    assert report.suggested is not None
    assert report.suggested["password"] == "***"
    assert report.to_patch() == {
        "host": "host.docker.internal",
        "port": 8123,
        "database": "property_intel",
    }
    assert report.attempted == len(report.attempts)
