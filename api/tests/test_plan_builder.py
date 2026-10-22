from __future__ import annotations

import json

import pytest

from app.agent.plan_builder import _coerce, build_plan
from app.agent.planner import SourceBinding


def _binding(**kw) -> SourceBinding:
    base = {
        "source_id": "c1",
        "name": "pg",
        "schema": "analytics",
        "table": "daily_revenue",
        "is_target": True,
        "row_count": 6,
        "primary_keys": ["order_id"],
        "columns": ["order_id", "customer_id", "amount", "status", "created_at"],
    }
    base.update(kw)
    return SourceBinding(**base)


def _binding2(**kw) -> SourceBinding:
    base = {
        "source_id": "c2",
        "name": "pg",
        "schema": "raw",
        "table": "customers",
        "is_target": False,
        "primary_keys": ["customer_id"],
        "columns": ["customer_id", "email"],
    }
    base.update(kw)
    return SourceBinding(**base)


# --- _coerce validation --------------------------------------------------- #


def test_check_with_known_column_is_kept() -> None:
    assert _coerce(
        {"kind": "distribution", "table": "daily_revenue", "column": "amount"}, [_binding()]
    )


def test_check_with_unknown_column_is_dropped() -> None:
    """The model invented `revenue`: it would only surface later as UndefinedColumn."""
    assert (
        _coerce(
            {"kind": "distribution", "table": "daily_revenue", "column": "revenue"}, [_binding()]
        )
        is None
    )


def test_check_for_unprofiled_binding_is_kept() -> None:
    """No profile = nothing to validate against; do not over-drop."""
    b = _binding(columns=None)
    assert _coerce({"kind": "distribution", "table": "daily_revenue", "column": "anything"}, [b])


def test_column_level_kind_without_column_is_dropped() -> None:
    assert _coerce({"kind": "null_rate", "table": "daily_revenue"}, [_binding()]) is None


def test_row_count_without_column_is_kept() -> None:
    assert _coerce({"kind": "row_count", "table": "daily_revenue"}, [_binding()])


def test_referential_with_unknown_parent_table_is_dropped() -> None:
    """The model pointed at `raw.orders` — no such binding was ever mapped."""
    raw = {
        "kind": "referential",
        "table": "daily_revenue",
        "source_id": "c1",
        "column": "customer_id",
        "params": {"parent_table": "raw.orders", "parent_column": "customer_id"},
    }
    assert _coerce(raw, [_binding(), _binding2()]) is None


def test_referential_with_unknown_parent_column_is_dropped() -> None:
    raw = {
        "kind": "referential",
        "table": "daily_revenue",
        "source_id": "c1",
        "column": "customer_id",
        "params": {"parent_table": "customers", "parent_column": "id"},
    }
    assert _coerce(raw, [_binding(), _binding2()]) is None


def test_referential_with_schema_qualified_parent_is_kept() -> None:
    raw = {
        "kind": "referential",
        "table": "daily_revenue",
        "source_id": "c1",
        "column": "customer_id",
        "params": {"parent_table": "raw.customers", "parent_column": "customer_id"},
    }
    check = _coerce(raw, [_binding(), _binding2()])
    assert check is not None
    assert check.params["parent_table"] == "raw.customers"


# --- build_plan merge ------------------------------------------------------ #


@pytest.mark.asyncio
async def test_build_plan_merges_valid_llm_checks_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the sound agent checks merge: unknown-column and column-less ones vanish."""
    b = _binding()
    response = json.dumps(
        {
            "summary": "Revenue integrity",
            "checks": [
                # sound extra
                {
                    "kind": "pattern",
                    "table": "daily_revenue",
                    "column": "status",
                    "regex": "^[A-Z_]+$",
                },
                # hallucinated column -> dropped
                {"kind": "distribution", "table": "daily_revenue", "column": "revenue"},
                # column-less -> dropped
                {"kind": "null_rate", "table": "daily_revenue"},
            ],
        }
    )
    monkeypatch.setattr("app.agent.plan_builder.llm_available", lambda: True)

    async def _ask(prompt: str, **_: object) -> str:
        return response

    monkeypatch.setattr("app.agent.plan_builder._ask_llm", _ask)

    plan = await build_plan(None, [b], use_llm=True)

    kinds = {(str(c.kind), c.column) for c in plan.checks}
    assert ("pattern", "status") in kinds
    assert ("distribution", "revenue") not in kinds
    assert not any(str(c.kind) == "null_rate" and c.column is None for c in plan.checks)
    assert "1 agent checks" in plan.summary
