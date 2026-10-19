"""The demo dataset is applied by the stack, so it has to stay correct.

These are not tests of the app — they are tests of the *promise* the README
makes: a fresh clone comes up with a deliberately broken dataset, without the
user running anything. If the SQL stops carrying its defects, or starts
dropping tables, the demo quietly stops being a demo.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# The demo SQL lives in the repo, outside the api package. It is mounted into
# the api container (/app/demo) so the suite guards the very files the stack
# seeds; on a host checkout it is found relative to this file.
_CANDIDATES = [
    Path("/app/demo"),
    Path(__file__).resolve().parents[2] / "scripts" / "demo",
]
DEMO_DIR = next((p for p in _CANDIDATES if p.is_dir()), None)
SQL_FILES = sorted(DEMO_DIR.glob("*.sql")) if DEMO_DIR else []


def _read(name: str) -> str:
    assert DEMO_DIR is not None, "the demo seed directory is not available"
    return (DEMO_DIR / name).read_text(encoding="utf-8")


def test_demo_sql_exists() -> None:
    assert DEMO_DIR is not None, (
        "the demo seed directory is missing — the stack has nothing to seed"
    )
    assert SQL_FILES, "scripts/demo/*.sql is missing — the stack has nothing to seed"
    assert {p.name for p in SQL_FILES} == {"10_dirty_orders.sql", "20_jaffle.sql"}


@pytest.mark.parametrize("path", SQL_FILES, ids=lambda p: p.name)
def test_demo_sql_never_drops(path: Path) -> None:
    """Seeding runs on every `up`; a DROP would wipe whatever is there."""
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"\bDROP\s+(TABLE|SCHEMA|DATABASE)\b", text, re.I), (
        f"{path.name} drops objects — the demo seed must only create what is missing"
    )


@pytest.mark.parametrize("path", SQL_FILES, ids=lambda p: p.name)
def test_demo_sql_is_idempotent(path: Path) -> None:
    """Re-running must be a no-op: every create is guarded, every insert is."""
    text = path.read_text(encoding="utf-8")
    for match in re.finditer(r"CREATE (TABLE|SCHEMA)\s+(?!IF NOT EXISTS)", text, re.I):
        raise AssertionError(f"{path.name}: unguarded CREATE at offset {match.start()}")
    inserts = re.findall(r"INSERT INTO\s+([\w.]+)", text, re.I)
    assert inserts, f"{path.name} inserts nothing"
    guarded = (
        "ON CONFLICT" in text.upper()
        or "WHERE NOT EXISTS" in text.upper()
    )
    assert guarded, f"{path.name} inserts without a guard — it would duplicate rows"


def test_dirty_dataset_carries_its_defects() -> None:
    """Each defect is one the README promises the agent will catch."""
    text = _read("10_dirty_orders.sql")
    # duplicate order_id 4, orphan customer_id 99, NULL amount, negative amount
    assert re.search(r"\(\s*4,\s*3,\s*102,\s*NULL", text), "the NULL amount row is gone"
    assert re.search(r"\(\s*4,\s*3,\s*102,\s*99\.99", text), "the duplicate order_id is gone"
    assert re.search(r"\(\s*5,\s*99,", text), "the orphan customer_id is gone"
    assert re.search(r"-20\.00", text), "the negative amount is gone"
    # a bad email and a NULL name
    assert "not-an-email" in text, "the invalid email is gone"
    assert re.search(r"\(\s*4,\s*NULL,", text), "the NULL customer name is gone"
    # freshness: every seeded timestamp must be older than the planner's 24h
    # SLA. A single fresh row would make the freshness check pass and quietly
    # break the demo.
    hours = [int(h) for h in re.findall(r"interval '(\d+) hours'", text)]
    assert hours, "the timestamps are not relative to the seed time"
    assert min(hours) > 24, (
        "a seeded row is fresh enough to pass the 24h freshness check"
    )


def test_jaffle_dataset_carries_its_defects() -> None:
    text = _read("20_jaffle.sql")
    assert re.search(r"\(\s*4,\s*3,\s*'2026-01-04'", text), "the orphan order is gone"
    assert re.search(r"\(\s*2,\s*'C',\s*NULL\s*\)", text), "the NULL last name is gone"
    assert "-5.00" in text, "the negative payment is gone"


def test_dirty_dataset_keeps_its_original_shape() -> None:
    """The table/column names are what the examples and docs refer to."""
    text = _read("10_dirty_orders.sql")
    for name in (
        "raw.customers",
        "raw.orders",
        "analytics.daily_revenue",
    ):
        assert name in text, f"{name} is missing from the demo seed"
    for column in ("customer_id", "order_id", "product_id", "amount", "status", "created_at"):
        assert column in text, f"column {column} is missing from the demo seed"


def test_jaffle_dataset_keeps_its_original_shape() -> None:
    """scripts/flow_check.py asks for these tables by name."""
    text = _read("20_jaffle.sql")
    for name in (
        "analytics.raw_customers",
        "analytics.raw_orders",
        "analytics.raw_payments",
    ):
        assert name in text, f"{name} is missing from the demo seed"


# --------------------------------------------------------------------------- #
# the SQL has to actually run
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", SQL_FILES, ids=lambda p: p.name)
def test_demo_sql_applies_to_a_real_database(path: Path) -> None:
    """Run the file for real, then roll it back.

    A regex can tell that a defect is mentioned; only the database can tell
    that the statement is valid SQL. The whole thing is rolled back, so the
    running stack is left exactly as it was.
    """
    from app.store.db import engine

    sql = path.read_text(encoding="utf-8")
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql(sql)
            conn.rollback()
    except Exception as exc:  # noqa: BLE001 - no database in this environment
        if _is_missing_database(exc):
            pytest.skip(f"no database available: {exc}")
        raise


def _is_missing_database(exc: Exception) -> bool:
    """A connection problem is a skip; a broken statement is a failure."""
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "could not connect",
            "connection refused",
            "name or service not known",
            "timeout",
            "no such host",
        )
    )
