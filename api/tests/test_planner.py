from __future__ import annotations

import pytest

from app.agent.planner import SourceBinding, plan_checks
from app.schemas import EtlAnalysis, Severity


def _binding(**kw) -> SourceBinding:
    base = {
        "source_id": "c1",
        "name": "pg",
        "schema": "analytics",
        "table": "daily_revenue",
        "is_target": True,
        "row_count": 1000,
        "primary_keys": ["order_id"],
        "columns": ["order_id", "customer_id", "amount", "status", "created_at"],
    }
    base.update(kw)
    return SourceBinding(**base)


def test_plan_covers_keys_nullability_and_baseline() -> None:
    checks = plan_checks(None, [_binding()])
    kinds = {str(c.kind) for c in checks}
    assert "unique" in kinds
    assert "null_rate" in kinds
    assert "row_count" in kinds
    assert any(c.severity == Severity.BLOCKER for c in checks)


def test_plan_adds_validity_checks_from_column_names() -> None:
    b = _binding(columns=["id", "email", "amount", "status"])
    names = " ".join(c.name for c in plan_checks(None, [b]))
    assert "email" in names
    assert "amount" in names


def test_plan_uses_profiled_null_rate_as_threshold() -> None:
    b = _binding(columns=["id", "email"])
    profile = {
        f"c1|{b.table}": {
            "columns": [
                {"name": "id", "data_type": "integer", "null_rate": 0.0},
                {"name": "email", "data_type": "text", "null_rate": 0.21},
            ]
        }
    }
    checks = {
        c.column: c
        for c in plan_checks(None, [b], profile=profile)
        if c.column and str(c.kind) == "null_rate"
    }
    assert checks["email"].threshold == 0.21
    assert "21" in (checks["email"].rationale or "")


def test_plan_flags_low_cardinality_categorical_as_drift_risk() -> None:
    b = _binding(columns=["id", "status"])
    profile = {
        f"c1|{b.table}": {
            "columns": [
                {"name": "id", "data_type": "integer", "null_rate": 0.0},
                {"name": "status", "data_type": "text", "null_rate": 0.0, "distinct_values": 3},
            ]
        }
    }
    checks = plan_checks(None, [b], profile=profile)
    assert any("cardinality is stable" in c.name for c in checks)


def test_plan_adds_row_count_parity_between_source_and_target() -> None:
    analysis = EtlAnalysis(
        language="sql",
        sources=["raw.orders"],
        targets=["analytics.daily_revenue"],
        # orders drives daily_revenue, so parity is meaningful
        grain={"daily_revenue": "raw.orders"},
    )
    bindings = [
        _binding(),
        SourceBinding(
            source_id="c1", name="pg", schema="raw", table="orders",
            is_target=False, row_count=1000, primary_keys=[], columns=["customer_id"],
        ),
    ]
    checks = plan_checks(analysis, bindings)
    parity = [c for c in checks if "parity" in c.tags]
    assert len(parity) == 1
    assert parity[0].params["expected_row_count"] == 1000
    # the baseline row_count probe must survive dedup alongside the parity probe
    assert any("baseline" in c.tags and str(c.kind) == "row_count" for c in checks)


def test_parity_is_skipped_when_the_driving_table_is_unknown() -> None:
    """Without grain evidence a parity check would compare mismatched grains."""
    analysis = EtlAnalysis(language="sql", sources=["raw.orders"], targets=["analytics.rev"])
    bindings = [
        SourceBinding(source_id="c1", name="pg", schema="analytics", table="daily_revenue",
                      is_target=True, row_count=6, primary_keys=[], columns=["order_id"]),
        SourceBinding(source_id="c1", name="pg", schema="raw", table="orders",
                      is_target=False, row_count=8, primary_keys=[], columns=["order_id"]),
    ]
    assert not [c for c in plan_checks(analysis, bindings) if "parity" in c.tags]


def test_parity_pairs_only_the_targets_own_driver() -> None:
    analysis = EtlAnalysis(
        language="sql",
        sources=["raw.orders", "raw.customers"],
        targets=["analytics.orders"],
        grain={"orders": "raw.orders"},
    )
    bindings = [
        SourceBinding(source_id="c1", name="pg", schema="analytics", table="orders",
                      is_target=True, row_count=8, primary_keys=[], columns=["id"]),
        SourceBinding(source_id="c1", name="pg", schema="raw", table="orders",
                      is_target=False, row_count=8, primary_keys=[], columns=["id"]),
        SourceBinding(source_id="c1", name="pg", schema="raw", table="customers",
                      is_target=False, row_count=3, primary_keys=[], columns=["id"]),
    ]
    parity = [c for c in plan_checks(analysis, bindings) if "parity" in c.tags]
    assert len(parity) == 1
    assert "raw_customers" not in parity[0].name


def test_plan_is_deterministic_and_deduped() -> None:
    a = plan_checks(None, [_binding()])
    b = plan_checks(None, [_binding()])
    keys_a = [(c.table.fqn, str(c.kind), c.column) for c in a]
    keys_b = [(c.table.fqn, str(c.kind), c.column) for c in b]
    assert keys_a == keys_b
    assert len(keys_a) == len(set(keys_a))


def test_plan_respects_max_checks() -> None:
    from app.core.config import settings

    many = _binding(columns=[f"c{i}" for i in range(300)])
    checks = plan_checks(None, [many])
    assert len(checks) <= settings.run_max_checks


@pytest.mark.parametrize("risk_text", ["SELECT *", "LEFT JOIN", "UNION ALL"])
def test_analysis_risks_are_surfaced(risk_text: str) -> None:
    from app.parsers import analyze_etl
    from app.schemas import EtlSubmit

    a = analyze_etl(
        EtlSubmit(language="sql", code=f"INSERT INTO t SELECT * FROM s {risk_text} s2 ON s.a=s2.a")
    )
    assert a.risks