from __future__ import annotations

import pytest

from app.schemas import EtlSubmit
from app.services.ingest import (
    extract_connections,
    guess_language,
    looks_like_code,
    parse_url,
)


def test_parse_postgres_url() -> None:
    spec = parse_url("postgresql://alice:s3cr3t@db.internal:5433/analytics?schema=public")
    assert spec is not None
    assert spec.type == "postgres"
    assert spec.host == "db.internal"
    assert spec.port == 5433
    assert spec.database == "analytics"
    assert spec.username == "alice"
    assert spec.password.get_secret_value() == "s3cr3t"
    assert spec.schema_ == "public"


def test_parse_mysql_url() -> None:
    spec = parse_url("mysql://root:pw@127.0.0.1:3306/shop")
    assert spec is not None and spec.type == "mysql"
    assert spec.port == 3306


def test_password_is_redacted() -> None:
    spec = parse_url("postgresql://u:secret@h:5432/d")
    assert spec is not None
    assert spec.redacted()["password"] == "***"


def test_extract_multiple_connections_from_chat_text() -> None:
    text = """
    Here is staging: postgresql://rw:pw1@stg.db:5432/warehouse
    and prod: mysql://ro:pw2@prod.db:3306/reports
    """
    specs = extract_connections(text)
    assert {str(s.type) for s in specs} == {"postgres", "mysql"}


def test_extract_json_connection_block() -> None:
    text = """use this one:
```json
{"type": "postgres", "name": "wh", "host": "h", "port": 5432,
 "database": "d", "username": "u", "password": "p"}
```"""
    specs = extract_connections(text)
    assert any(s.name == "wh" and s.host == "h" for s in specs)


def test_extract_bare_credentials() -> None:
    text = "the postgres host is warehouse.internal port 5432 database dw user analyst password hunter2"
    specs = extract_connections(text)
    assert specs, "expected a spec from bare credentials"
    s = specs[0]
    assert s.host == "warehouse.internal"
    assert s.database == "dw"
    assert s.password.get_secret_value() == "hunter2"


def test_guess_language() -> None:
    assert str(guess_language("SELECT a FROM b JOIN c ON 1=1")) == "sql"
    assert str(guess_language("import pandas as pd\npd.read_sql(x)")) == "python"
    assert str(guess_language("{{ ref('stg_orders') }}")) == "dbt"


def test_looks_like_code() -> None:
    assert looks_like_code("SELECT id, amount FROM raw.orders WHERE amount > 0 LIMIT 10")
    assert not looks_like_code("hello there, can you run the audit please?")
    assert not looks_like_code("SELECT")  # too short to be a real script


def test_etl_submit_roundtrip() -> None:
    submit = EtlSubmit(language="sql", code="SELECT 1")
    assert submit.language == "sql"

def test_from_url_connects_when_parseable() -> None:
    """A URL-shaped input parses and *attempts* a connection (which then fails)."""
    from fastapi import HTTPException

    from app.routers.connections import FromUrlRequest, connect_from_url

    try:
        connect_from_url(
            FromUrlRequest(url="postgresql://u:p@no-such-host.invalid:5432/db")
        )
    except HTTPException as exc:
        # only the parse failure surfaces as 422; DNS failure is a red ok=False
        raise AssertionError(f"unexpected 422 for a valid URL shape: {exc.detail}") from exc
    except Exception:
        pass  # any driver/DNS error means parsing succeeded and connect was tried


def test_from_url_rejects_unsupported_scheme_with_clear_message() -> None:
    from fastapi import HTTPException

    from app.routers.connections import FromUrlRequest, connect_from_url

    with pytest.raises(HTTPException) as exc:
        connect_from_url(FromUrlRequest(url="mongodb://u:p@host/db"))
    assert exc.value.status_code == 422
    assert "mongodb" in exc.value.detail
    assert "postgres" in exc.value.detail  # lists what IS supported


def test_from_url_rejects_non_url_with_clear_message() -> None:
    from fastapi import HTTPException

    from app.routers.connections import FromUrlRequest, connect_from_url

    with pytest.raises(HTTPException) as exc:
        connect_from_url(FromUrlRequest(url="just some words about my database"))
    assert exc.value.status_code == 422
    assert "connection URL" in exc.value.detail


# --- templated dbt YAML values ---------------------------------------------- #


def test_jinja_value_recovers_env_var_defaults() -> None:
    from app.services.ingest import jinja_value

    assert jinja_value("{{ env_var('CLICKHOUSE_PORT', '8123') | int }}") == "8123"
    assert jinja_value('{{ env_var("PG_HOST", "db.internal") }}') == "db.internal"
    assert jinja_value("{{ env_var('X', '9000') | as_number }}") == "9000"
    # dbt documents the keyword spelling too
    assert jinja_value("{{ env_var('X', default='8123') }}") == "8123"
    assert jinja_value("{{ env_var('X', default='8123') | int }}") == "8123"


def test_jinja_value_drops_unrecoverable_templates() -> None:
    from app.services.ingest import jinja_value

    # no default to recover
    assert jinja_value("{{ env_var('CLICKHOUSE_HOST') }}") is None
    # conditionals / macros cannot be evaluated from the file alone
    assert jinja_value("{{ 'raw' if target.name == 'prod' else 'raw_dev' }}") is None
    assert jinja_value("{{ var('schema') }}") is None
    # never hand the UI a string that still contains {{
    assert "{{" not in str(jinja_value("host-{{ env_var('SUFFIX') }}"))


def test_jinja_value_plain_passthrough() -> None:
    from app.services.ingest import jinja_value

    assert jinja_value("ch.internal") == "ch.internal"
    assert jinja_value("  quoted  ") == "quoted"
    assert jinja_value('"quoted2"') == "quoted2"
    assert jinja_value(8123) == 8123
    assert jinja_value(None) is None


def test_coerce_port() -> None:
    from app.services.ingest import coerce_port

    assert coerce_port("{{ env_var('PORT', '8123') | int }}") == 8123
    assert coerce_port("5432") == 5432
    assert coerce_port(5432) == 5432
    assert coerce_port("not-a-port") is None
    assert coerce_port(0) is None
    assert coerce_port(70000) is None
    assert coerce_port(None) is None
