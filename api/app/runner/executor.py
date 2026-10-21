"""Executes a TestPlan against live data sources and scores the outcome."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

from app.connectors.base import Connector, ConnectorError
from app.connectors.factory import build_connector
from app.core.config import settings
from app.core.logging import log
from app.runner.compiler import (
    UnsafeSQLError,
    assert_readonly,
    compile_check,
    sample_failure_sql,
    severity_weight,
)
from app.schemas import (
    CheckResult,
    CheckSpec,
    CheckStatus,
    ConnectionSpec,
    QualityScore,
    RunResult,
    RunStatus,
    TestPlan,
)


class TestRunner:
    def __init__(
        self,
        connections: dict[str, ConnectionSpec],
        progress: Any = None,
    ) -> None:
        self._conns = connections
        self._progress = progress
        self._live: dict[str, Connector] = {}

    # -- lifecycle ---------------------------------------------------------- #
    def _connector(self, source_id: str) -> Connector:
        if source_id not in self._live:
            spec = self._conns.get(source_id)
            if spec is None:
                raise ConnectorError(f"unknown source_id: {source_id}")
            self._live[source_id] = build_connector(spec)
        return self._live[source_id]

    def close(self) -> None:
        for c in self._live.values():
            c.dispose()
        self._live.clear()

    async def __aenter__(self) -> TestRunner:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.close()

    # -- execution ---------------------------------------------------------- #
    async def run(
        self, run_id: str, plan: TestPlan, expected_row_counts: dict[str, int] | None = None
    ) -> RunResult:
        started = datetime.now(UTC)
        t0 = time.perf_counter()
        result = RunResult(id=run_id, status=RunStatus.RUNNING, plan=plan, started_at=started)
        checks = plan.checks[: settings.run_max_checks]
        sem = asyncio.Semaphore(settings.run_max_concurrency)

        await self._emit("run.started", {"run_id": run_id, "checks": len(checks)})

        async def run_one(check: CheckSpec) -> CheckResult:
            async with sem:
                r = await asyncio.to_thread(self._execute_check, check, expected_row_counts or {})
                await self._emit(
                    "check.completed",
                    {
                        "run_id": run_id,
                        "check_id": check.id,
                        "name": check.name,
                        "status": str(r.status),
                        "severity": str(check.severity),
                        "observed": r.observed,
                        "expected": r.expected,
                        "message": r.message,
                        "duration_ms": r.duration_ms,
                    },
                )
                return r

        results = await asyncio.gather(*(run_one(c) for c in checks))
        result.results = list(results)
        result.score = self._score(results)
        result.duration_ms = round((time.perf_counter() - t0) * 1000, 2)
        result.finished_at = datetime.now(UTC)
        result.status = RunStatus.COMPLETED
        await self._emit(
            "run.completed",
            {
                "run_id": run_id,
                "score": result.score.overall,
                "passed": result.score.passed,
                "failed": result.score.failed,
                "errored": result.score.errored,
                "duration_ms": result.duration_ms,
            },
        )
        return result

    async def _emit(self, event: str, data: dict) -> None:
        if self._progress is None:
            return
        try:
            res = self._progress(event, data)
            if asyncio.iscoroutine(res):
                await res
        except Exception as exc:  # noqa: BLE001 - never fail a run over telemetry
            log.debug("progress emit failed", event=event, error=str(exc))

    # -- single check ------------------------------------------------------- #
    def _execute_check(self, check: CheckSpec, expected_counts: dict[str, int]) -> CheckResult:
        start = time.perf_counter()
        base = CheckResult(
            check_id=check.id,
            name=check.name,
            kind=check.kind,
            table=check.table,
            column=check.column,
            severity=check.severity,
        )
        try:
            conn = self._connector(check.table.source_id)
            sql, params = compile_check(check, conn)

            if str(check.kind) == "schema":
                cols = conn.describe_table(check.table.schema_, check.table.name)
                expected = check.params.get("expected_columns")
                observed = sorted(c.name for c in cols)
                ok = (
                    list(expected) == observed
                    if isinstance(expected, list)
                    else set(expected or observed) == set(observed)
                )
                base.status = CheckStatus.PASSED if ok else CheckStatus.FAILED
                base.observed = str(observed)
                base.expected = str(sorted(expected or []))
                base.message = (
                    "schema matches contract"
                    if ok
                    else f"schema drift: observed {observed} vs expected {sorted(expected or [])}"
                )
                base.duration_ms = round((time.perf_counter() - start) * 1000, 2)
                return base

            assert_readonly(sql)
            res = conn.query(sql, params)
            row = res.rows[0] if res.rows else {}
            self._evaluate(base, check, row, expected_counts)
            base.query = sql[:500]
            base.duration_ms = res.duration_ms

            if base.status == CheckStatus.FAILED and not base.sample_rows:
                base.sample_rows = self._sample(conn, check)

        except UnsafeSQLError as exc:
            base.status = CheckStatus.ERROR
            base.message = f"unsafe or invalid query: {exc}"
        except ConnectorError as exc:
            base.status = CheckStatus.ERROR
            base.message = str(exc)[:400]
        except Exception as exc:  # noqa: BLE001
            base.status = CheckStatus.ERROR
            base.message = f"{type(exc).__name__}: {exc}"[:400]
            log.warning("check failed", check=check.name, error=str(exc))

        return base

    def _evaluate(
        self, base: CheckResult, check: CheckSpec, row: dict, expected_counts: dict[str, int]
    ) -> None:
        kind = str(check.kind)
        total = float(row.get("total") or row.get("row_count") or 0) or 1.0

        def status(ok: bool) -> CheckStatus:
            return CheckStatus.PASSED if ok else CheckStatus.FAILED

        match kind:
            case "null_rate":
                nulls = float(row.get("nulls") or 0)
                rate = nulls / total
                thr = check.threshold or 0.0
                base.observed = round(rate * 100, 3)
                base.expected = f"<= {thr * 100:.3f}% nulls"
                base.failed_rows = int(nulls)
                base.status = status(rate <= thr)
                base.message = (
                    f"{nulls:,.0f}/{int(total):,} nulls ({rate:.3%})" if nulls else "no nulls"
                )

            case "unique":
                distinct = float(row.get("distinct_values") or 0)
                dupes = total - distinct
                base.observed = int(dupes)
                base.expected = "0 duplicate values"
                base.failed_rows = int(dupes)
                base.status = status(dupes == 0)
                base.message = (
                    f"{dupes:,.0f} duplicate value(s) across {total:,.0f} row(s)"
                    if dupes
                    else "all values unique"
                )

            case "duplicate":
                extra = float(row.get("extra_rows") or 0)
                base.observed = int(extra)
                base.expected = "0 duplicated rows"
                base.failed_rows = int(extra)
                base.status = status(extra == 0)
                base.message = f"{extra:,.0f} surplus duplicate row(s)"

            case "row_count" | "volume":
                rc = float(row.get("row_count") or 0)
                key = f"{check.table.source_id}|{check.table.fqn}"
                expected = check.params.get("expected_row_count") or expected_counts.get(key)
                base.observed = int(rc)
                if expected is None:
                    # No baseline yet: the probe itself succeeded, and the
                    # observed count becomes the baseline for future runs.
                    base.status = CheckStatus.PASSED
                    base.expected = "baseline recorded"
                    base.message = f"{int(rc):,} rows (no baseline to compare against yet)"
                    return
                tol = check.tolerance_ratio or 0.0
                lo, hi = expected * (1 - tol), expected * (1 + tol)
                base.observed = int(rc)
                base.expected = f"{int(expected):,} (+/-{tol:.1%})"
                base.status = status(lo <= rc <= hi)
                delta = rc - expected
                base.message = (
                    f"{int(rc):,} rows vs expected {int(expected):,} "
                    f"(delta {delta:+,.0f}, tolerance {tol:.1%})"
                )

            case "referential":
                orphans = float(row.get("orphans") or 0)
                thr = check.threshold or 0.0
                base.observed = int(orphans)
                base.expected = f"<= {thr} orphan row(s)"
                base.failed_rows = int(orphans)
                base.status = status(orphans <= thr)
                base.message = (
                    f"{orphans:,.0f}/{int(total):,} orphan row(s) "
                    f"(no parent in {check.params.get('parent_table')})"
                )

            case "freshness":
                base.observed = str(row.get("latest"))
                base.expected = f"max({check.column}) within SLA"
                latest = row.get("latest")
                base.status = CheckStatus.PASSED
                base.message = f"latest record: {latest}"
                if latest:
                    try:
                        ts = (
                            latest
                            if isinstance(latest, datetime)
                            else datetime.fromisoformat(str(latest))
                        )
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=UTC)
                        age_h = (datetime.now(UTC) - ts).total_seconds() / 3600
                        sla = float(check.params.get("sla_hours") or 24)
                        base.message += f" (age {age_h:.1f}h, SLA {sla}h)"
                        base.status = status(age_h <= sla)
                    except Exception:  # noqa: BLE001
                        base.message += " (age not computable)"

            case "min_max":
                mn, mx = row.get("min_value"), row.get("max_value")
                base.observed = f"{mn} .. {mx}"
                limits = []
                if check.min_value is not None:
                    limits.append(f">= {check.min_value}")
                if check.max_value is not None:
                    limits.append(f"<= {check.max_value}")
                ok = True
                if check.min_value is not None and mn is not None:
                    ok &= float(mn) >= float(check.min_value)
                if check.max_value is not None and mx is not None:
                    ok &= float(mx) <= float(check.max_value)
                base.expected = " and ".join(limits) if limits else "observed min/max"
                base.status = status(ok)

            case "range":
                violations = float(row.get("violations") or 0)
                base.observed = int(violations)
                bounds = []
                if check.min_value is not None:
                    bounds.append(f">= {check.min_value}")
                if check.max_value is not None:
                    bounds.append(f"<= {check.max_value}")
                base.expected = " and ".join(bounds) if bounds else "no bounds"
                base.failed_rows = int(violations)
                base.status = status(violations == 0)
                base.message = (
                    f"{violations:,.0f}/{int(total):,} value(s) violate "
                    f"{check.column} {' '.join(bounds)}".strip()
                )

            case "pattern":
                violations = float(row.get("violations") or 0)
                base.observed = int(violations)
                base.expected = f"matches /{check.regex}/"
                base.failed_rows = int(violations)
                base.status = status(violations == 0)
                base.message = f"{violations:,.0f}/{int(total):,} value(s) violate the pattern"

            case "distribution":
                non_null = float(row.get("non_null") or 0)
                distinct = float(row.get("distinct_values") or 0)
                base.observed = {
                    "null_rate": round(1 - non_null / total, 4),
                    "distinct_values": int(distinct),
                    "min": row.get("min_value"),
                    "max": row.get("max_value"),
                }
                base.expected = "profile snapshot (baseline for drift detection)"
                base.status = CheckStatus.PASSED
                base.message = f"{non_null:,.0f} non-null, {distinct:,.0f} distinct"

            case "custom_sql":
                val = next(iter(row.values()), None) if row else None
                base.observed = val
                base.expected = "1 / true"
                ok = val in (1, True, "1", "true", "t") or bool(val) and str(val).lower() == "true"
                base.status = status(bool(ok))
                base.message = f"custom assertion returned {val!r}"

    def _sample(self, conn: Connector, check: CheckSpec) -> list[dict[str, Any]]:
        try:
            sql = sample_failure_sql(check, conn)
            assert_readonly(sql)
            return conn.query(sql).rows[: settings.run_max_row_sample]
        except Exception as exc:  # noqa: BLE001 - sampling is best effort
            log.debug("sample fetch failed", check=check.name, error=str(exc))
            return []

    # -- scoring ------------------------------------------------------------ #
    def _score(self, results: list[CheckResult]) -> QualityScore:
        total_weight = 0.0
        passed_weight = 0.0
        by_sev_pass: dict[str, float] = {}
        by_sev_total: dict[str, float] = {}
        # keyed by the QualityScore field name, not the enum value:
        # CheckStatus.ERROR renders as "error" but the field is `errored`.
        counts = {"passed": 0, "failed": 0, "errored": 0, "skipped": 0}
        status_field = {
            str(CheckStatus.PASSED): "passed",
            str(CheckStatus.FAILED): "failed",
            str(CheckStatus.ERROR): "errored",
            str(CheckStatus.SKIPPED): "skipped",
        }

        for r in results:
            sev = str(r.severity)
            w = severity_weight(r.severity)
            by_sev_total[sev] = by_sev_total.get(sev, 0.0) + w
            field = status_field.get(str(r.status))
            if field:
                counts[field] += 1
            if r.status in (CheckStatus.PASSED, CheckStatus.SKIPPED):
                passed_weight += w
                by_sev_pass[sev] = by_sev_pass.get(sev, 0.0) + w
            total_weight += w

        overall = round(100.0 * passed_weight / total_weight, 2) if total_weight else 100.0
        by_sev = {
            sev: round(100.0 * by_sev_pass.get(sev, 0.0) / tot, 2)
            for sev, tot in by_sev_total.items()
            if tot
        }
        return QualityScore(
            overall=overall,
            by_severity=by_sev,
            passed=counts["passed"],
            failed=counts["failed"],
            errored=counts["errored"],
            skipped=counts["skipped"],
        )


async def execute_plan(
    run_id: str,
    plan: TestPlan,
    connections: dict[str, ConnectionSpec],
    progress: Any = None,
) -> RunResult:
    async with TestRunner(connections, progress=progress) as runner:
        return await runner.run(run_id, plan)
