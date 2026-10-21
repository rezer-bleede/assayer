from __future__ import annotations

from app.connectors.base import Connector, ConnectorError, QueryResult
from app.schemas import ColumnInfo, ConnectionSpec, SourceType


class FakeConnector(Connector):
    """In-memory connector used to test the executor without a real database."""

    source_type = SourceType.SQLITE
    default_schema = "main"

    def __init__(self, spec: ConnectionSpec, data: dict[str, list[dict]]) -> None:
        super().__init__(spec)
        self.data = data
        self.executed: list[str] = []

    def _engine(self):  # pragma: no cover - unused
        return None

    def list_schemas(self) -> list[str]:
        return ["main"]

    def list_tables(self, schema: str | None = None) -> list[str]:
        return list(self.data)

    def describe_table(self, schema: str | None, table: str) -> list[ColumnInfo]:
        rows = self.data.get(table, [])
        if not rows:
            return []
        return [
            ColumnInfo(
                name=key,
                data_type="integer" if isinstance(val, int) else "text",
                nullable=val is not None,
            )
            for key, val in rows[0].items()
        ]

    def primary_keys(self, schema: str | None, table: str) -> list[str]:
        return ["id"] if "id" in (self.data.get(table) or [{}])[0] else []

    def query(self, sql: str, params: dict | None = None) -> QueryResult:
        self.executed.append(sql)
        low = sql.lower()
        rows = self._respond(low, sql)
        return QueryResult(
            columns=list(rows[0].keys()) if rows else [], rows=rows, rowcount=len(rows)
        )

    def _respond(self, low: str, sql: str) -> list[dict]:
        table = "orders" if "orders" in low else "events"
        data = self.data.get(table, [])
        total = len(data)
        nulls = sum(1 for r in data if r.get("email") is None)
        distinct = len({r.get("id") for r in data})
        if "nulls" in low:
            return [{"total": total, "nulls": nulls}]
        if "distinct_values" in low and "count(distinct" in low:
            return [{"total": total, "distinct_values": distinct}]
        if "extra_rows" in low:
            extra = total - distinct
            return [{"extra_rows": max(extra, 0), "max_dupes": 2 if extra else 0}]
        if "row_count" in low or "count(*) as row_count" in low:
            return [{"row_count": total}]
        if "orphans" in low:
            return [{"total": total, "orphans": 1}]
        if "violations" in low and "!~" in low:
            return [{"total": total, "violations": 2}]
        if "violations" in low:
            return [{"total": total, "violations": 0}]
        if "latest" in low:
            return [{"total": total, "latest": "2026-10-16T00:00:00+00:00", "earliest": None}]
        if "min_value" in low:
            return [{"min_value": 0, "max_value": 1000}]
        if "distinct_values" in low:
            return [
                {
                    "total": total,
                    "non_null": total - nulls,
                    "distinct_values": 3,
                    "min_value": 0,
                    "max_value": 100,
                }
            ]
        return [{"total": total}]


async def test_runner_scores_and_flags_failures(monkeypatch) -> None:
    from app.runner import executor as exec_mod
    from app.schemas import CheckKind, CheckSpec, TableRef, TestPlan

    spec = ConnectionSpec(type="sqlite", name="test", path=":memory:")
    conn = FakeConnector(
        spec,
        {
            "orders": [
                {"id": 1, "email": None, "amount": 10},
                {"id": 1, "email": "a@b.co", "amount": -5},
                {"id": 3, "email": "bad", "amount": 20},
            ]
        },
    )
    monkeypatch.setattr(exec_mod, "build_connector", lambda s: conn)

    plan = TestPlan(
        checks=[
            CheckSpec(
                name="table readable",
                kind=CheckKind.ROW_COUNT,
                table=TableRef(source_id="c1", schema="main", name="orders"),
            ),
            CheckSpec(
                name="email not null",
                kind=CheckKind.NULL_RATE,
                table=TableRef(source_id="c1", schema="main", name="orders"),
                column="email",
                threshold=0.0,
            ),
            CheckSpec(
                name="id unique",
                kind=CheckKind.UNIQUE,
                table=TableRef(source_id="c1", schema="main", name="orders"),
                column="id",
            ),
            CheckSpec(
                name="amount non-negative",
                kind=CheckKind.RANGE,
                table=TableRef(source_id="c1", schema="main", name="orders"),
                column="amount",
                min_value=0,
                max_value=None,
            ),
        ]
    )

    events: list[str] = []
    result = await exec_mod.execute_plan(
        "run1", plan, {"c1": spec}, progress=lambda e, d: events.append(e)
    )

    by_name = {r.name: r for r in result.results}
    assert by_name["table readable"].status == "passed"
    assert by_name["email not null"].status == "failed"
    assert by_name["id unique"].status == "failed"
    assert by_name["amount non-negative"].status == "passed"

    assert result.score.failed == 2
    assert result.score.passed == 2
    assert 0 < result.score.overall < 100
    assert "run.started" in events and "run.completed" in events
    assert result.duration_ms is not None


async def test_runner_marks_invalid_check_as_error(monkeypatch) -> None:
    from app.runner import executor as exec_mod
    from app.schemas import CheckKind, CheckSpec, TableRef, TestPlan

    spec = ConnectionSpec(type="sqlite", name="test", path=":memory:")
    monkeypatch.setattr(
        exec_mod, "build_connector", lambda s: FakeConnector(spec, {"orders": [{"id": 1}]})
    )
    plan = TestPlan(
        checks=[
            CheckSpec(
                name="evil custom sql",
                kind=CheckKind.CUSTOM_SQL,
                table=TableRef(source_id="c1", name="orders"),
                sql="DELETE FROM orders",
            )
        ]
    )
    result = await exec_mod.execute_plan("run2", plan, {"c1": spec})
    assert result.results[0].status == "error"
    assert "unsafe" in (result.results[0].message or "").lower()


def test_connector_error_type() -> None:
    assert issubclass(ConnectorError, RuntimeError)
