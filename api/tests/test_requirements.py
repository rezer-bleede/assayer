"""Tests for requirement derivation — grouping ETL tables into per-database checks."""

from __future__ import annotations

from app.schemas import (
    EtlAnalysis,
    SourceType,
)
from app.services.requirements import (
    derive_requirements,
    extract_connection_hints,
    split_ref,
)


def _analysis(
    *,
    sources: list[str] | None = None,
    targets: list[str] | None = None,
    table_hints: dict[str, dict] | None = None,
    connection_hints: list[dict] | None = None,
    raw_code: str = "",
) -> EtlAnalysis:
    return EtlAnalysis(
        language="sql",
        sources=sources or [],
        targets=targets or [],
        table_hints=table_hints or {},
        connection_hints=connection_hints or [],
        raw_code=raw_code,
    )


def test_engine_hint_survives_unknown_adapter() -> None:
    """dbt names adapters we cannot connect to; that must not kill the parse."""
    from app.services.requirements import _engine_type

    assert _engine_type("clickhouse") is None
    assert _engine_type("redshift") is None
    assert _engine_type("postgres") is SourceType.POSTGRES
    assert _engine_type("") is None
    assert _engine_type(None) is None


def test_derive_requirements_keeps_host_for_unknown_adapter() -> None:
    analysis = _analysis(
        sources=["raw.orders"],
        connection_hints=[
            {
                "type": "clickhouse",
                "host": "ch.internal",
                "port": 9000,
                "database": "analytics",
                "schema": "raw",
                "username": "selector",
            }
        ],
    )
    reqs = derive_requirements(analysis)
    assert len(reqs) == 1
    assert reqs[0].engine_hint is None
    assert reqs[0].host_hint == "ch.internal"
    assert reqs[0].port_hint == 9000
    assert reqs[0].user_hint == "selector"
    # the database evidence still groups the tables correctly
    assert reqs[0].database == "analytics"


def test_split_ref() -> None:
    assert split_ref("raw.orders") == ("raw", "orders")
    assert split_ref("analytics.daily_revenue") == ("analytics", "daily_revenue")
    assert split_ref("orders") == (None, "orders")
    assert split_ref('"analytics"."daily_revenue"') == ("analytics", "daily_revenue")
    assert split_ref("`raw`.orders") == ("raw", "orders")


def test_derive_requirements_single_database() -> None:
    """Source + target in the same schema → one requirement, mixed role."""
    analysis = _analysis(
        sources=["raw.orders", "raw.customers"],
        targets=["analytics.daily_revenue"],
        table_hints={
            "raw.orders": {"database": "warehouse", "schema": "raw"},
            "raw.customers": {"database": "warehouse", "schema": "raw"},
            "analytics.daily_revenue": {"database": "warehouse", "schema": "analytics"},
        },
    )
    reqs = derive_requirements(analysis)
    assert len(reqs) == 1
    assert reqs[0].key == "db:warehouse"
    assert set(reqs[0].tables) == {"raw.customers", "raw.orders", "analytics.daily_revenue"}
    assert reqs[0].role == "mixed"
    assert reqs[0].database == "warehouse"


def test_derive_requirements_separate_databases() -> None:
    """Source in `raw`, target in `analytics` → two requirements."""
    analysis = _analysis(
        sources=["raw.orders"],
        targets=["analytics.daily_revenue"],
        table_hints={
            "raw.orders": {"database": "staging", "schema": "raw"},
            "analytics.daily_revenue": {"database": "production", "schema": "analytics"},
        },
    )
    reqs = derive_requirements(analysis)
    assert len(reqs) == 2
    by_key = {r.key: r for r in reqs}
    assert by_key["db:staging"].role == "source"
    assert by_key["db:production"].role == "target"
    assert by_key["db:staging"].tables == ["raw.orders"]
    assert by_key["db:production"].tables == ["analytics.daily_revenue"]


def test_derive_requirements_connection_hint_overrides_schema() -> None:
    """Connection string found in code should prefill database/host."""
    analysis = _analysis(
        sources=["raw.orders"],
        targets=["analytics.daily_revenue"],
        connection_hints=[
            {
                "type": "postgres",
                "host": "prod-db.example.com",
                "port": 5432,
                "database": "prod",
                "schema": "analytics",
                "username": "app_user",
            }
        ],
        table_hints={
            "raw.orders": {"database": "prod", "schema": "raw"},
            "analytics.daily_revenue": {"database": "prod", "schema": "analytics"},
        },
    )
    reqs = derive_requirements(analysis)
    assert len(reqs) == 1
    assert reqs[0].database == "prod"
    assert reqs[0].engine_hint == SourceType.POSTGRES
    assert reqs[0].host_hint == "prod-db.example.com"
    assert reqs[0].port_hint == 5432
    assert reqs[0].user_hint == "app_user"
    assert reqs[0].origin == "engine url in code"
    assert reqs[0].confidence == 0.9


def test_derive_requirements_dbt_hints() -> None:
    """dbt sources.yml hints drive database for grouping."""
    analysis = _analysis(
        sources=["raw_orders"],
        targets=["fct_revenue"],
        table_hints={
            "raw_orders": {
                "database": "raw_db",
                "schema": "raw",
                "table": "orders",
                "origin": "dbt sources.yml",
            },
        },
    )
    reqs = derive_requirements(analysis)
    assert len(reqs) == 2
    assert reqs[0].database == "raw_db"
    assert reqs[0].origin == "dbt profiles"
    assert reqs[0].confidence == 0.9



def test_derive_requirements_empty() -> None:
    analysis = _analysis()
    assert derive_requirements(analysis) == []


def test_derive_requirements_single_table() -> None:
    analysis = _analysis(
        sources=["raw.orders"],
        targets=[],
        table_hints={"raw.orders": {"database": "staging", "schema": "raw"}},
    )
    reqs = derive_requirements(analysis)
    assert len(reqs) == 1
    assert reqs[0].role == "source"
    assert reqs[0].tables == ["raw.orders"]


def test_extract_connection_hints() -> None:
    code = """
engine = create_engine("postgresql://app_user:s3cr3t@prod-db.example.com:5432/prod")
spark = SparkSession.builder.config("spark.sql.warehouse.dir", "s3a://warehouse/").getOrCreate()
"""
    hints = extract_connection_hints(code)
    assert len(hints) == 1
    assert hints[0]["type"] == "postgres"
    assert hints[0]["host"] == "prod-db.example.com"
    assert hints[0]["port"] == 5432
    assert "prod" in hints[0]["database"]
    assert hints[0]["username"] == "app_user"
    assert "password" not in hints[0]
    assert "s3cr3t" not in str(hints)


def test_extract_connection_hints_empty() -> None:
    assert extract_connection_hints("") == []
    assert extract_connection_hints("SELECT 1") == []