"""ClickHouse connector + dialect tests.

The driver is stubbed (`sys.modules` injection): these tests pin *what SQL we
send* and *what client settings we open* — never a live server. analytics-ch
lives on a user intranet; CI has no ClickHouse and must not need one.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from app.connectors.base import ConnectorError
from app.connectors.clickhouse import ClickHouseConnector
from app.runner.compiler import compile_check, sample_failure_sql
from app.schemas import CheckKind, CheckSpec, ConnectionSpec, SourceType, TableRef


class _Result:
    def __init__(self, columns: list[str], rows: list[tuple]) -> None:
        self.result_columns = columns
        self.result_rows = rows


class _FakeClient:
    """Records every query, answers the ones introspection needs."""

    def __init__(self) -> None:
        self.queries: list[str] = []
        self.closed = False

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> _Result:
        self.queries.append(sql)
        if "system.databases" in sql:
            return _Result(["name"], [("raw",), ("analytics",), ("system",)])
        if "system.tables" in sql:
            return _Result(["name"], [("orders",)])
        if "system.columns" in sql and "is_in_primary_key" in sql:
            return _Result(["name"], [("order_id",)])
        if "system.columns" in sql:
            return _Result(
                ["name", "type", "is_nullable"],
                [("order_id", "Int64", 0), ("email", "Nullable(String)", 1)],
            )
        if "COUNT(*)" in sql:
            return _Result(["c"], [(42,)])
        return _Result(["a"], [(1,), (2,)])

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def fake_driver(monkeypatch):
    calls: dict[str, Any] = {}
    client = _FakeClient()

    def get_client(**kwargs: Any) -> _FakeClient:
        calls.update(kwargs)
        return client

    mod = types.ModuleType("clickhouse_connect")
    mod.get_client = get_client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "clickhouse_connect", mod)
    return calls, client


def _spec(**kw: Any) -> ConnectionSpec:
    base: dict[str, Any] = {
        "type": SourceType.CLICKHOUSE,
        "name": "ch",
        "host": "ch.internal",
        "port": 8123,
        "database": "warehouse",
        "username": "analyst",
        "password": "s3cret",
    }
    base.update(kw)
    return ConnectionSpec(**base)


def _conn() -> ClickHouseConnector:
    conn = ClickHouseConnector(_spec())
    conn._client = _FakeClient()  # type: ignore[assignment]
    return conn


# --- client construction ---------------------------------------------------- #


def test_client_opens_readonly_session(fake_driver) -> None:
    calls, _client = fake_driver
    conn = ClickHouseConnector(_spec())
    conn.query("SELECT 1")
    assert calls["host"] == "ch.internal"
    assert calls["port"] == 8123
    assert calls["username"] == "analyst"
    assert calls["password"] == "s3cret"
    assert calls["database"] == "warehouse"
    assert calls["settings"] == {"readonly": 2}


def test_secure_flag_from_options_and_port(fake_driver) -> None:
    calls, _ = fake_driver
    ClickHouseConnector(_spec(options={"secure": "true"})).query("SELECT 1")
    assert calls["secure"] is True
    ClickHouseConnector(_spec(port=8443))._connection()
    assert calls["secure"] is True


def test_native_port_failure_names_the_http_interface(fake_driver, monkeypatch) -> None:
    """A refused connection on the native TCP port must explain *why* it can't
    ever work with this driver — not just surface a socket error."""
    def refusing_get_client(**kwargs: Any) -> _FakeClient:
        raise ConnectionError(
            "HTTPConnection(host='localhost', port=9002): Connection refused"
        )

    mod = types.ModuleType("clickhouse_connect")
    mod.get_client = refusing_get_client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "clickhouse_connect", mod)

    conn = ClickHouseConnector(_spec(port=9002))
    with pytest.raises(ConnectorError) as exc:
        conn.query("SELECT 1")
    assert "9002" in str(exc.value)
    assert "HTTP" in str(exc.value)
    assert "8123" in str(exc.value)

    # the same failure on the HTTP port does not get the (wrong) hint
    mod_http = types.ModuleType("clickhouse_connect")
    mod_http.get_client = refusing_get_client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "clickhouse_connect", mod_http)
    conn = ClickHouseConnector(_spec(port=9100))
    with pytest.raises(ConnectorError) as exc:
        conn.query("SELECT 1")
    assert "8123" not in str(exc.value)


# --- environmental hints ------------------------------------------------------ #


def test_localhost_refusal_explains_the_container_boundary(fake_driver, monkeypatch) -> None:
    """Refusing localhost is a container problem, and the error must say so."""
    def refusing_get_client(**kwargs: Any) -> _FakeClient:
        raise ConnectionError("HTTPConnection(host='localhost', port=8123): "
                              "Failed to establish a new connection: Connection refused")

    mod = types.ModuleType("clickhouse_connect")
    mod.get_client = refusing_get_client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "clickhouse_connect", mod)

    status = ClickHouseConnector(_spec(host="localhost", port=8123)).test()
    assert status.ok is False
    assert "host.docker.internal" in (status.message or "")

    # a refusal from a named remote host is a normal failure: no container hint
    mod2 = types.ModuleType("clickhouse_connect")
    mod2.get_client = refusing_get_client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "clickhouse_connect", mod2)
    status = ClickHouseConnector(_spec(host="ch.internal", port=8123)).test()
    assert status.ok is False
    assert "host.docker.internal" not in (status.message or "")


# --- introspection shape ----------------------------------------------------- #


def test_introspection_uses_system_tables(fake_driver) -> None:
    _calls, client = fake_driver
    conn = _conn()
    conn._client = client
    # the *SQL* carries the exclusions — a real server applies them, a stub
    # cannot, so assert on the query we emit, not on fake filtering
    conn.list_schemas()
    last = client.queries[-1]
    assert "system.databases" in last and "NOT IN" in last and "'system'" in last
    conn.list_tables("raw")
    assert "system.tables" in client.queries[-1] and "raw" in client.queries[-1]
    cols = conn.describe_table("raw", "orders")
    assert cols[0].data_type == "Int64"
    assert cols[0].nullable is False
    assert cols[1].data_type == "String"  # Nullable() unwrapped
    assert cols[1].nullable is True
    assert conn.primary_keys("raw", "orders") == ["order_id"]
    assert "is_in_primary_key" in client.queries[-1]
    assert conn.row_count("raw", "orders") == 42


def test_query_caps_and_shaped_result(fake_driver) -> None:
    conn = _conn()
    res = conn.query("SELECT a FROM t")
    assert res.rows == [{"a": 1}, {"a": 2}]
    assert res.columns == ["a"]


def test_params_are_refused_not_ignored(fake_driver) -> None:
    conn = _conn()
    with pytest.raises(ConnectorError, match="inline"):
        conn.query("SELECT {x}", {"x": 1})


def test_driver_1_11_result_shape(fake_driver) -> None:
    """clickhouse-connect 1.x moved the named columns to `column_names`;
    `result_columns` is now the column-major *values*. Both shapes must zip."""
    class _NewShape:
        column_names = ("a", "b")
        result_columns = [[1, 2], ["x", "y"]]  # values, not names
        result_rows = [(1, "x"), (2, "y")]

    class _FakeNew(_FakeClient):
        def query(self, sql: str, parameters: dict[str, Any] | None = None) -> Any:
            return _NewShape()

    conn = _conn()
    conn._client = _FakeNew()  # type: ignore[assignment]
    res = conn.query("SELECT a, b FROM t")
    assert res.columns == ["a", "b"]
    assert res.rows == [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]


def test_dispose_closes_client(fake_driver) -> None:
    _calls, client = fake_driver
    conn = ClickHouseConnector(_spec())
    conn.query("SELECT 1")
    conn.dispose()
    assert client.closed is True


# --- dialect ----------------------------------------------------------------- #


def test_quote_table_is_db_dot_table() -> None:
    assert _conn().quote_table("raw", "orders") == '"raw"."orders"'
    assert _conn().quote_table(None, "orders") == '"warehouse"."orders"'


def test_regex_mismatch_is_match_function_with_doubled_backslashes() -> None:
    out = _conn().regex_mismatch('"email"', r"^[^@\s]+@[^@\s]+$")
    assert out == "NOT match(\"email\", '^[^@\\\\s]+@[^@\\\\s]+$')"


def test_compiler_pattern_check_uses_dialect(fake_driver) -> None:
    check = CheckSpec(
        name="p",
        kind=CheckKind.PATTERN,
        table=TableRef(source_id="c1", schema="raw", name="orders"),
        column="email",
        regex=r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$",
    )
    sql, _ = compile_check(check, _conn())
    assert "NOT match(" in sql
    assert "!~" not in sql
    assert "\\\\s" in sql  # doubled

    sample = sample_failure_sql(check, _conn())
    assert "NOT match(" in sample
    assert "!~" not in sample


def test_postgres_default_pattern_unchanged() -> None:
    """The base dialect is byte-for-byte the old compiler output."""
    from app.connectors.base import Connector
    from app.schemas import ColumnInfo  # noqa: F401 - import hygiene only

    class Pg(Connector):
        source_type = SourceType.POSTGRES

        def _engine(self):
            return None

        def list_schemas(self):
            return []

        def list_tables(self, schema=None):
            return []

        def describe_table(self, schema, table):
            return []

        def primary_keys(self, schema, table):
            return []

        def query(self, sql, params=None):
            raise AssertionError("not used")

    out = Pg(_spec(type=SourceType.POSTGRES)).regex_mismatch('"email"', r"^\d+$")
    assert out == "\"email\" !~ '^\\d+$'"
