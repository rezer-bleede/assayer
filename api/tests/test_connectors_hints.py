"""Environmental failure hints (container boundary around localhost).

The agent runs inside a container; a refusal against `localhost` is almost
always a user whose database lives on the host machine. These tests pin that
explanation appearing at test() time for every connector family.
"""

from __future__ import annotations

from typing import Any

from app.connectors.base import _friendly_error, localhost_hint
from app.schemas import ConnectionSpec, SourceType


def _spec(**kw: Any) -> ConnectionSpec:
    base: dict[str, Any] = {
        "type": SourceType.POSTGRES,
        "name": "pg",
        "host": "db.internal",
        "port": 5432,
        "database": "db",
    }
    base.update(kw)
    return ConnectionSpec(**base)


class _Refused(Exception):
    def __str__(self) -> str:
        return "(psycopg) connection to server at 'localhost' (127.0.0.1), port 5432 failed: Connection refused"


def test_localhost_refusal_gets_the_container_hint() -> None:
    hint = localhost_hint(_spec(host="localhost", port=5432), str(_Refused()))
    assert hint is not None
    assert "host.docker.internal" in hint


def test_remote_refusal_gets_no_container_hint() -> None:
    assert localhost_hint(_spec(host="db.internal", port=5432), str(_Refused())) is None


def test_localhost_unrelated_error_gets_no_hint() -> None:
    assert localhost_hint(_spec(host="localhost", port=5432), "password authentication failed") is None


def test_loopback_variants_count() -> None:
    for host in ("localhost", "127.0.0.1", "LOCALHOST", "::1", "0.0.0.0"):
        hint = localhost_hint(_spec(host=host, port=5432), "Connection refused")
        assert hint is not None, host


def test_friendly_error_appends_hint_when_spec_passed() -> None:
    msg = _friendly_error(_Refused(), spec=_spec(host="localhost", port=5432))
    assert "host.docker.internal" in msg
    msg = _friendly_error(_Refused())  # no spec: plain text, no crash
    assert "host.docker.internal" not in msg


def test_sqlalchemy_test_surfaces_the_hint(monkeypatch) -> None:
    import app.connectors.sqlalchemy_connector as sa_mod

    def refusing_create_engine(*args: Any, **kwargs: Any) -> Any:
        raise _Refused()

    monkeypatch.setattr(sa_mod, "create_engine", refusing_create_engine)
    from app.connectors.sqlalchemy_connector import SqlAlchemyConnector

    status = SqlAlchemyConnector(_spec(host="localhost", port=5432)).test()
    assert status.ok is False
    assert "host.docker.internal" in (status.message or "")
