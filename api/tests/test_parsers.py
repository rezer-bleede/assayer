from __future__ import annotations

import pathlib

import pytest

from app.parsers import analyze_etl
from app.schemas import EtlSubmit

SQL = """
WITH paid AS (
  SELECT order_id, customer_id, amount FROM raw.orders WHERE status = 'paid'
)
INSERT INTO analytics.daily_revenue (order_id, customer_id, amount)
SELECT p.order_id, p.customer_id, SUM(p.amount) AS amount
FROM paid p
JOIN raw.customers c ON c.id = p.customer_id
GROUP BY p.order_id, p.customer_id
"""

PY = """
import pandas as pd

orders = pd.read_sql("SELECT * FROM raw.orders", conn)
customers = pd.read_sql("SELECT * FROM raw.customers", conn)
merged = orders.merge(customers, on="customer_id", how="left")
totals = merged.groupby("customer_id").agg(amount=("amount", "sum"))
totals.to_sql("customer_totals", engine, if_exists="append", index=False)
"""

DBT = """
with source as (
    select * from {{ source('raw', 'orders') }}
),
renamed as (
    select order_id, customer_id, amount as order_amount
    from source
    where amount is not null
)
select * from renamed
"""


def test_sql_lineage_and_transformations() -> None:
    a = analyze_etl(EtlSubmit(language="sql", code=SQL))
    joined = " ".join(a.sources + a.targets)
    assert "raw.orders" in joined or "orders" in joined
    assert "customers" in joined
    assert any(t.kind == "cte" for t in a.transformations)
    assert any(t.kind == "join" for t in a.transformations)
    assert any(t.kind == "filter" for t in a.transformations)
    assert any(t.kind == "aggregation" for t in a.transformations)
    assert a.lineage, "expected lineage edges"


def test_sql_detects_risks() -> None:
    risky = "INSERT INTO tgt SELECT * FROM src_a LEFT JOIN src_b ON src_a.id = src_b.id"
    a = analyze_etl(EtlSubmit(language="sql", code=risky))
    assert any("SELECT *" in r for r in a.risks)
    assert any("LEFT JOIN" in r for r in a.risks)


def test_python_lineage_and_risks() -> None:
    a = analyze_etl(EtlSubmit(language="python", code=PY))
    kinds = {t.kind for t in a.transformations}
    assert {"sql_source", "write", "join", "aggregation"} <= kinds
    assert "customer_totals" in " ".join(a.targets)
    assert any("merge" in r.lower() for r in a.risks)


def test_python_syntax_error_is_reported_not_raised() -> None:
    a = analyze_etl(EtlSubmit(language="python", code="def broken(:\n"))
    assert any(t.kind == "parse_error" for t in a.transformations)


def test_dbt_ref_and_source_extraction() -> None:
    a = analyze_etl(EtlSubmit(language="dbt", files={"models/rev.sql": DBT}))
    assert any(t.kind == "source" for t in a.transformations)
    assert "raw.orders" in a.sources


def test_dbt_manifest_lineage() -> None:
    manifest = {
        "nodes": {
            "model.proj.stg_orders": {
                "resource_type": "model",
                "name": "stg_orders",
                "depends_on": {"nodes": ["source.proj.raw.orders"]},
                "columns": {
                    "id": {"name": "id", "data_tests": ["unique", "not_null"]},
                    "amount": {"name": "amount"},
                },
                "config": {"materialized": "table"},
            },
            "model.proj.fct_revenue": {
                "resource_type": "model",
                "name": "fct_revenue",
                "depends_on": {"nodes": ["model.proj.stg_orders"]},
                "columns": {},
                "config": {"materialized": "incremental"},
            },
        },
        "sources": {
            "source.proj.raw.orders": {
                "source_name": "raw",
                "name": "orders",
                "identifier": "orders",
            }
        },
        "seeds": {},
    }
    a = analyze_etl(EtlSubmit(language="dbt", manifest=manifest))
    assert set(a.targets) == {"stg_orders", "fct_revenue"}
    assert any(t.kind == "dbt_test" for t in a.transformations)
    assert any(e.target == "stg_orders" and e.source == "raw.orders" for e in a.lineage)
    assert any("Incremental" in r for r in a.risks)


# --- python lineage depth ------------------------------------------------ #

PY_CHAIN = """
import pandas as pd
orders = pd.read_sql("SELECT * FROM raw.orders", engine)
customers = pd.read_sql("SELECT * FROM raw.customers", engine)
merged = orders.merge(customers[["customer_id", "country"]], on="customer_id", how="left")
totals = merged.groupby(["customer_id"], dropna=False).agg(amount=("amount", "sum")).reset_index()
totals = totals.drop_duplicates()
totals.to_sql("customer_totals", engine, if_exists="append", index=False)
"""


def test_python_lineage_resolves_through_a_method_chain() -> None:
    """`totals = merged.groupby(..).agg(..).reset_index()` must keep lineage."""
    a = analyze_etl(EtlSubmit(language="python", code=PY_CHAIN))
    edges = {(e.source, e.target) for e in a.lineage}
    assert ("raw.orders", "customer_totals") in edges
    assert ("raw.customers", "customer_totals") in edges
    assert a.targets == ["customer_totals"]


def test_python_sink_is_not_mistaken_for_a_source() -> None:
    a = analyze_etl(EtlSubmit(language="python", code=PY_CHAIN))
    assert "customer_totals" not in a.sources


def test_python_subscripted_argument_resolves_to_its_variable() -> None:
    a = analyze_etl(EtlSubmit(language="python", code=PY_CHAIN))
    joins = a.joins
    assert joins and joins[0]["right_table"] == "customers"
    assert joins[0]["on"] == "customer_id"
    assert joins[0]["how"] == "left"


def test_python_aggregations_use_the_same_shape_as_sql() -> None:
    a = analyze_etl(EtlSubmit(language="python", code=PY_CHAIN))
    assert a.aggregations
    assert all("function" in agg for agg in a.aggregations)


def test_python_detects_data_cleaning_as_a_risk() -> None:
    a = analyze_etl(EtlSubmit(language="python", code=PY_CHAIN))
    joined = " ".join(a.risks).lower()
    assert "drop_duplicates" in joined
    assert "append" in joined
    kinds = {t.kind for t in a.transformations}
    assert "sanitise" in kinds
    assert "write" in kinds


def test_python_spark_style_sink_is_detected() -> None:
    code = """
df = spark.table("raw.events")
df.write.mode("overwrite").saveAsTable("analytics.events_flat")
"""
    a = analyze_etl(EtlSubmit(language="python", code=code))
    assert "raw.events" in a.sources
    assert any(t.kind == "write" for t in a.transformations)


# --- unparseable SQL must still yield usable lineage ---------------------- #


def test_sql_fallback_still_reports_sources_and_targets() -> None:
    """Regression: the regex fallback returned empty sources/targets."""
    # `#` is not a SQL comment, so sqlglot rejects this
    code = "# bad comment\nCREATE TABLE analytics.rev AS\nSELECT * FROM raw.orders o JOIN raw.c c ON o.id=c.id"
    a = analyze_etl(EtlSubmit(language="sql", code=code))
    assert a.targets, "fallback must still name the target table"
    assert a.sources, "fallback must still name the source tables"
    assert a.lineage


def _find_examples() -> pathlib.Path | None:
    for parent in pathlib.Path(__file__).resolve().parents:
        candidate = parent / "examples"
        if candidate.is_dir():
            return candidate
    return None


def test_demo_example_files_are_valid_sql() -> None:
    """The shipped demo must parse cleanly — `#` comments are not SQL."""
    examples = _find_examples()
    if examples is None:
        pytest.skip("examples/ not mounted")
    demo = examples / "demo_daily_revenue.sql"
    assert demo.exists()
    a = analyze_etl(EtlSubmit(language="sql", code=demo.read_text()))
    assert a.sources == ["raw.orders", "raw.customers"], a.sources
    assert a.targets == ["analytics.daily_revenue"], a.targets


# --- dbt YAML with Jinja templates ------------------------------------------- #

_TEMPLATED_PROFILES = """
warehouse:
  target: prod
  outputs:
    prod:
      type: clickhouse
      host: "{{ env_var('CLICKHOUSE_HOST', 'analytics-ch.internal') }}"
      port: "{{ env_var('CLICKHOUSE_PORT', '8123') | int }}"
      user: "{{ env_var('CLICKHOUSE_USER') }}"
      password: "{{ env_var('CLICKHOUSE_PW') }}"
      database: warehouse
      schema: raw
      secure: True
"""

_TEMPLATED_SOURCES = """
version: 2
sources:
  - name: raw
    database: "{{ env_var('RAW_DB', 'warehouse') }}"
    schema: "{{ env_var('RAW_SCHEMA', 'raw') }}"
    tables:
      - name: orders
      - name: events
        identifier: "{{ env_var('EV_TABLE') }}"
"""


def test_dbt_hints_recovers_env_var_defaults() -> None:
    from app.parsers.dbt_parser import dbt_hints

    hints, conns = dbt_hints(
        {"shop/profiles.yml": _TEMPLATED_PROFILES, "shop/models/schema.yml": _TEMPLATED_SOURCES}
    )
    conn = conns[0]
    assert conn["host"] == "analytics-ch.internal"
    assert conn["port"] == 8123
    assert conn["database"] == "warehouse"
    assert conn["schema"] == "raw"
    # a template with no default is dropped entirely, never forwarded raw
    assert "username" not in conn
    # passwords never travel in hints
    joined = " ".join(str(v) for h in hints.values() for v in h.values()) + " " + str(conns)
    assert "CLICKHOUSE_PW" not in joined
    assert "{{" not in joined
    assert hints["raw.orders"]["database"] == "warehouse"
    assert hints["raw.events"]["table"] == "events"  # unresolvable identifier -> name


def test_dbt_hints_plain_values_unchanged() -> None:
    from app.parsers.dbt_parser import dbt_hints

    _hints, conns = dbt_hints({"profiles.yml": _TEMPLATED_PROFILES})
    assert conns[0]["port"] == 8123
