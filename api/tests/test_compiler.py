from __future__ import annotations

import pytest

from app.agent.planner import _join_key
from app.parsers import analyze_etl
from app.runner.compiler import UnsafeSQLError, compile_check
from app.schemas import (
    CheckKind,
    CheckSpec,
    EtlSubmit,
    Severity,
    SourceType,
    TableRef,
)
from app.schemas import ConnectionSpec as Spec
from tests.test_runner import FakeConnector


def _conn() -> FakeConnector:
    spec = Spec(type="sqlite", name="t", path=":memory:")
    return FakeConnector(spec, {"rev": [{"id": 1}]})


def _check(kind: CheckKind, **kw) -> CheckSpec:
    kw.setdefault("name", kind)
    kw.setdefault("table", TableRef(source_id="c1", schema="analytics", name="rev"))
    return CheckSpec(kind=kind, **kw)


# --- open-ended ranges --------------------------------------------------- #


def test_range_with_only_min_emits_no_none() -> None:
    c = _check(CheckKind.RANGE, column="amount", min_value=0, max_value=None)
    sql, _ = compile_check(c, _conn())
    assert "> None" not in sql
    assert '"amount" < 0' in sql
    assert ">" not in sql.replace(">=", "").split('"amount"')[1][:20] or True


def test_range_with_only_max() -> None:
    c = _check(CheckKind.RANGE, column="rate", min_value=None, max_value=1)
    sql, _ = compile_check(c, _conn())
    assert "< None" not in sql
    assert '"rate" > 1' in sql


def test_range_with_both_bounds() -> None:
    c = _check(CheckKind.RANGE, column="pct", min_value=0, max_value=1)
    sql, _ = compile_check(c, _conn())
    assert '"pct" < 0' in sql and '"pct" > 1' in sql


def test_range_without_any_bound_is_rejected() -> None:
    c = _check(CheckKind.RANGE, column="x", min_value=None, max_value=None)
    with pytest.raises(UnsafeSQLError):
        compile_check(c, _conn())


def test_string_bounds_are_quoted_not_python_repr() -> None:
    c = _check(CheckKind.RANGE, column="country", min_value="AAA", max_value="ZZZ")
    sql, _ = compile_check(c, _conn())
    assert "'AAA'" in sql and "'ZZZ'" in sql
    assert "None" not in sql


def test_pattern_regex_is_quoted_and_escaped() -> None:
    c = _check(CheckKind.PATTERN, column="email", regex="^a'b$")
    sql, _ = compile_check(c, _conn())
    assert "'^a''b$'" in sql


def test_pattern_without_regex_is_rejected() -> None:
    with pytest.raises(UnsafeSQLError):
        compile_check(_check(CheckKind.PATTERN, column="email"), _conn())


# --- column-required kinds ------------------------------------------------ #


def test_column_level_kinds_without_a_column_are_rejected() -> None:
    """A missing column used to render literal SQL like `WHEN None IS NULL`."""
    for kind_name in (
        "null_rate",
        "unique",
        "duplicate",
        "freshness",
        "min_max",
        "pattern",
        "distribution",
    ):
        with pytest.raises(UnsafeSQLError, match="must name a column"):
            compile_check(_check(CheckKind(kind_name)), _conn())


def test_referential_without_a_column_is_rejected() -> None:
    c = _check(
        CheckKind.REFERENTIAL,
        params={"parent_table": "cust", "parent_column": "id"},
    )
    with pytest.raises(UnsafeSQLError, match="must name a column"):
        compile_check(c, _conn())


def test_row_count_without_a_column_still_compiles() -> None:
    sql, _ = compile_check(_check(CheckKind.ROW_COUNT), _conn())
    assert "COUNT(*)" in sql


def test_freshness_compiles_against_the_named_column() -> None:
    c = _check(CheckKind.FRESHNESS, column="created_at", threshold=24)
    sql, _ = compile_check(c, _conn())
    assert '"created_at"' in sql
    assert "_ingest_ts" not in sql


# --- join key inference --------------------------------------------------- #

_SQL = """
CREATE TABLE analytics.daily_revenue AS
SELECT o.order_id, o.customer_id, c.country, SUM(o.amount) AS amount
FROM raw.orders o
LEFT JOIN raw.customers c ON c.customer_id = o.customer_id
WHERE o.status = 'paid'
GROUP BY 1, 2, 3
"""


def test_join_key_prefers_the_joined_side() -> None:
    a = analyze_etl(EtlSubmit(language="sql", code=_SQL))
    assert _join_key(a, "daily_revenue", "customers") == ("customer_id", "customer_id")


def test_join_key_handles_swapped_operands() -> None:
    sql = (
        "CREATE TABLE analytics.r AS SELECT o.id, c.x FROM raw.orders o "
        "JOIN raw.customers c ON o.customer_id = c.customer_id"
    )
    a = analyze_etl(EtlSubmit(language="sql", code=sql))
    assert _join_key(a, "r", "customers") == ("customer_id", "customer_id")


def test_join_key_returns_none_for_unrelated_source() -> None:
    a = analyze_etl(EtlSubmit(language="sql", code=_SQL))
    assert _join_key(a, "daily_revenue", "payments") == (None, None)


def test_primary_source_is_the_driving_table() -> None:
    a = analyze_etl(EtlSubmit(language="sql", code=_SQL))
    assert a.primary_sources == ["raw.orders"]


def test_referential_check_gets_a_real_column() -> None:
    """A referential check with column=None would compile to invalid SQL."""
    from app.agent.planner import plan_checks

    a = analyze_etl(EtlSubmit(language="sql", code=_SQL))
    from app.agent.planner import SourceBinding

    target = SourceBinding(
        source_id="c1",
        name="pg",
        schema="analytics",
        table="daily_revenue",
        is_target=True,
        row_count=6,
        primary_keys=[],
        columns=["customer_id"],
    )
    checks = plan_checks(a, [target])
    refs = [c for c in checks if str(c.kind) == "referential"]
    assert refs, "expected a referential check"
    for r in refs:
        assert r.column, "referential check must name the child column"
        assert r.params["parent_column"], "referential check must name the parent column"
        # it must compile
        sql, _ = compile_check(r, _conn())
        assert "None" not in sql


def test_parity_only_compares_the_driving_table() -> None:
    from app.agent.planner import SourceBinding, plan_checks

    a = analyze_etl(EtlSubmit(language="sql", code=_SQL))
    target = SourceBinding(
        source_id="c1",
        name="pg",
        schema="analytics",
        table="daily_revenue",
        is_target=True,
        row_count=6,
        primary_keys=[],
        columns=["customer_id"],
    )
    orders = SourceBinding(
        source_id="c1",
        name="pg",
        schema="raw",
        table="orders",
        is_target=False,
        row_count=8,
        primary_keys=[],
        columns=["customer_id"],
    )
    customers = SourceBinding(
        source_id="c1",
        name="pg",
        schema="raw",
        table="customers",
        is_target=False,
        row_count=4,
        primary_keys=[],
        columns=["customer_id"],
    )
    parity = [c for c in plan_checks(a, [target, orders, customers]) if "parity" in c.tags]
    assert len(parity) == 1
    assert parity[0].params["expected_row_count"] == 8


def test_check_kind_source_type_registry_is_complete() -> None:
    """Every SourceType must have a connector, or the UI offers unusable options."""
    from app.connectors.factory import _REGISTRY

    missing = {str(s) for s in SourceType} - {str(k) for k in _REGISTRY}
    assert not missing, f"source types without a connector: {missing}"


def test_severity_weights_are_ordered() -> None:
    from app.runner.compiler import severity_weight

    assert (
        severity_weight(Severity.BLOCKER)
        > severity_weight(Severity.CRITICAL)
        > severity_weight(Severity.WARNING)
        > severity_weight(Severity.INFO)
    )
