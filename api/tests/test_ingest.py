from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.schemas import EtlSubmit, SourceType
from app.services.ingest import (
    extract_connections,
    extract_connections_async,
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

async def test_from_url_connects_when_parseable() -> None:
    """A URL-shaped input parses and *attempts* a connection (which then fails)."""
    from fastapi import HTTPException

    from app.routers.connections import FromUrlRequest, connect_from_url

    try:
        await connect_from_url(
            FromUrlRequest(url="postgresql://u:p@no-such-host.invalid:5432/db")
        )
    except HTTPException as exc:
        # only the parse failure surfaces as 422; DNS failure is a red ok=False
        raise AssertionError(f"unexpected 422 for a valid URL shape: {exc.detail}") from exc
    except Exception:
        pass  # any driver/DNS error means parsing succeeded and connect was tried


async def test_from_url_rejects_unsupported_scheme_with_clear_message() -> None:
    from fastapi import HTTPException

    from app.routers.connections import FromUrlRequest, connect_from_url

    with pytest.raises(HTTPException) as exc:
        await connect_from_url(FromUrlRequest(url="mongodb://u:p@host/db"))
    assert exc.value.status_code == 422
    assert "mongodb" in exc.value.detail
    assert "postgres" in exc.value.detail  # lists what IS supported


async def test_from_url_rejects_non_url_with_clear_message() -> None:
    from fastapi import HTTPException

    from app.routers.connections import FromUrlRequest, connect_from_url

    with pytest.raises(HTTPException) as exc:
        await connect_from_url(FromUrlRequest(url="just some words about my database"))
    assert exc.value.status_code == 422
    assert "connection URL" in exc.value.detail


def test_parse_clickhouse_url() -> None:
    spec = parse_url(
        "clickhouse://analyst:s3cret@analytics-ch.internal:8123/warehouse?secure=true"
    )
    assert spec is not None
    assert spec.type is SourceType.CLICKHOUSE
    assert spec.host == "analytics-ch.internal"
    assert spec.port == 8123
    assert spec.database == "warehouse"
    assert spec.options == {"secure": "true"}
    assert spec.password.get_secret_value() == "s3cret"


def test_clickhouse_word_guess() -> None:
    from app.services.ingest import guess_type

    assert guess_type("our events live in clickhouse on the intranet") is (
        SourceType.CLICKHOUSE
    )


def test_diagnosis_lists_clickhouse_as_supported() -> None:
    from app.routers.connections import _url_diagnosis

    msg = _url_diagnosis("redshift://u:p@rs.internal/db")
    assert "redshift" in msg and "unsupported" in msg.lower()
    msg = _url_diagnosis("clickhouse://u@h/db")
    assert "Unsupported connection scheme" not in msg


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


# --------------------------------------------------------------------------- #
# engine word evidence + client-constructor extraction
# --------------------------------------------------------------------------- #


def test_engine_word_guess_single_family() -> None:
    from app.services.ingest import engine_word_guess

    assert engine_word_guess("client = clickhouse_connect.get_client(...)") is (
        SourceType.CLICKHOUSE
    )
    assert engine_word_guess("we log into postgresql nightly") is SourceType.POSTGRES
    assert engine_word_guess("duckdb import duckdb") is SourceType.SQLITE


def test_engine_word_guess_rejects_ambiguity_and_silence() -> None:
    from app.services.ingest import engine_word_guess

    # two different engines mentioned -> no honest answer
    assert engine_word_guess("postgres on prod, clickhouse on analytics") is None
    assert engine_word_guess("select 1") is None
    assert engine_word_guess("") is None


def test_engine_word_guess_module_names_and_extensions() -> None:
    from app.services.ingest import engine_word_guess

    # module names count as evidence
    assert engine_word_guess("import clickhouse_connect") is SourceType.CLICKHOUSE
    # longer identifiers after the engine name are not engine mentions
    assert engine_word_guess("the clickhousedb project docs") is None


_CLIENT_SNIPPET = """
import clickhouse_connect
client = clickhouse_connect.get_client(
    host='warehouse.internal',
    port=8123,
    username='analyst',
    database='property_intel',
    password='s3cret',
)
"""


def test_get_client_literal_call_yields_redacted_spec() -> None:
    specs = extract_connections(_CLIENT_SNIPPET)
    spec = next(s for s in specs if s.type is SourceType.CLICKHOUSE)
    assert spec.host == "warehouse.internal"
    assert spec.port == 8123
    assert spec.username == "analyst"
    assert spec.database == "property_intel"
    # secrets inside the user's code are never lifted into a spec
    assert spec.password is None


def test_get_client_requires_a_literal_host() -> None:
    from app.services.ingest import _client_call_specs

    # unquoted runtime variables: we cannot know their values, so no spec
    assert _client_call_specs("clickhouse_connect.get_client(host=CH_HOST, port=PORT)") == []
    # no host at all: nothing to connect to
    assert _client_call_specs("clickhouse_connect.get_client(database='d')") == []


# --------------------------------------------------------------------------- #
# New tests: trailing-text URL, LLM fallback
# --------------------------------------------------------------------------- #


def test_parse_url_strips_trailing_prose_from_database() -> None:
    """Trailing natural-language text after the DB name must not leak in."""
    # Simulates:  clickhouse://host/property_intelI expect: my_schema.table
    spec = parse_url("clickhouse://analyst:pw@ch.internal:8123/property_intelI")
    # The regex lookahead should stop at the camelCase boundary before 'I'
    assert spec is not None
    assert spec.database is not None
    assert spec.database == "property_intel", (
        f"Expected 'property_intel', got {spec.database!r}"
    )


def test_extract_connections_trailing_sentence() -> None:
    """Full URL followed immediately by prose doesn't corrupt the database field."""
    text = "clickhouse://u:pw@ch.host:8123/analytics_db  I expect this to be the main DB."
    specs = extract_connections(text)
    assert specs, "expected at least one spec"
    # The database must not include the trailing sentence
    assert specs[0].database == "analytics_db"


async def test_extract_connections_async_delegates_deterministic() -> None:
    """When deterministic parsing succeeds, async wrapper returns same results."""
    text = "postgresql://u:pw@db.host:5432/mydb"
    specs = await extract_connections_async(text)
    assert specs
    assert specs[0].type == SourceType.POSTGRES
    assert specs[0].database == "mydb"


async def test_extract_connections_async_llm_fallback() -> None:
    """When deterministic parsing fails, the LLM is invoked for unstructured text.

    We stub ``app.core.llm`` in ``sys.modules`` so this test does not require
    ``structlog`` (which is an optional dependency in the test environment).
    """
    import sys
    import types

    import app.services.ingest as ingest_mod

    freeform = (
        "Hey, our analytics cluster is clickhouse running on analytics.internal "
        "port 9000 database property_intel user analyst password s3cret."
    )

    llm_response = (
        '[{"type": "clickhouse", "name": "ch-llm", "host": "analytics.internal", '
        '"port": 9000, "database": "property_intel", "username": "analyst", '
        '"password": "s3cret"}]'
    )

    # Build a minimal fake app.core.llm module
    fake_llm = types.ModuleType("app.core.llm")
    fake_llm.llm_available = lambda: True  # type: ignore[attr-defined]
    fake_llm.ainvoke = AsyncMock(return_value=llm_response)  # type: ignore[attr-defined]

    # Inject the stub before the local import inside extract_connections_async runs
    original = sys.modules.get("app.core.llm", None)
    sys.modules["app.core.llm"] = fake_llm
    try:
        with patch.object(ingest_mod, "extract_connections", return_value=[]):
            specs = await ingest_mod.extract_connections_async(freeform)
    finally:
        if original is None:
            del sys.modules["app.core.llm"]
        else:
            sys.modules["app.core.llm"] = original

    assert specs, "expected LLM extraction to yield at least one spec"
    assert specs[0].type == SourceType.CLICKHOUSE
    assert specs[0].host == "analytics.internal"
    assert specs[0].database == "property_intel"
