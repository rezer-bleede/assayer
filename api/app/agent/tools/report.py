"""Audit report generation: structured summary + optional LLM narrative."""

from __future__ import annotations

from collections.abc import Callable

from app.agent.prompts import report_prompt
from app.core.llm import ThoughtCallback, astream, llm_available
from app.core.logging import log
from app.schemas import CheckStatus, EtlAnalysis, RunResult


def structured_summary(run: RunResult) -> dict:
    failed = [r for r in run.results if r.status == CheckStatus.FAILED]
    errored = [r for r in run.results if r.status == CheckStatus.ERROR]
    grouped: dict[str, list[dict]] = {}
    for r in failed + errored:
        grouped.setdefault(str(r.severity), []).append(
            {
                "check": r.name,
                "table": r.table.fqn if r.table else (r.url or "web app"),
                "column": r.column,
                "observed": r.observed,
                "expected": r.expected,
                "message": r.message,
                "samples": r.sample_rows[:3],
            }
        )
    return {
        "run_id": run.id,
        "score": run.score.overall,
        "by_severity": run.score.by_severity,
        "passed": run.score.passed,
        "failed": run.score.failed,
        "errored": run.score.errored,
        "skipped": run.score.skipped,
        "duration_ms": run.duration_ms,
        "failures": grouped,
    }


def _fallback_narrary(run: RunResult) -> str:
    s = run.score
    verdict = "healthy" if s.overall >= 95 else "degraded" if s.overall >= 75 else "broken"
    lines = [
        f"**Verdict** — target is **{verdict}** (QA score {s.overall}/100).",
        "",
        f"{s.passed} passed · {s.failed} failed · {s.errored} errored · {s.skipped} skipped "
        f"in {run.duration_ms or 0}ms.",
    ]
    failures = [r for r in run.results if r.status == CheckStatus.FAILED]
    if failures:
        lines += ["", "**What broke**"]
        for r in failures[:10]:
            lines.append(
                f"- `{r.table.fqn if r.table else (r.url or 'web app')}`{f'.{r.column}' if r.column else ''} — "
                f"{r.name}: observed `{r.observed}`, expected `{r.expected}`"
            )
    errors = [r for r in run.results if r.status == CheckStatus.ERROR]
    if errors:
        lines += ["", "**Could not evaluate**"]
        for r in errors[:5]:
            lines.append(f"- {r.name}: {r.message}")
    if not failures and not errors:
        lines += ["", "No issues detected. All executed checks passed."]
    return "\n".join(lines)


def _tie_to_etl(run: RunResult, analysis: EtlAnalysis | None) -> list[str]:
    if analysis is None:
        return []
    hints: list[str] = []
    failed_tables = {
        r.table.fqn.split(".")[-1].lower() for r in run.results if r.status == CheckStatus.FAILED and r.table
    }
    for t in analysis.transformations:
        target = (t.target or "").split(":")[-1].split(".")[-1].lower()
        if target in failed_tables:
            hints.append(f"{t.kind}: {t.description}")
    return hints[:10]


async def build_report(
    run: RunResult,
    analysis: EtlAnalysis | None = None,
    on_delta: Callable[[str], None] | None = None,
    on_thought: ThoughtCallback | None = None,
    project_context: str | None = None,
) -> str:
    summary = structured_summary(run)
    if not llm_available():
        return _fallback_narrary(run)

    etl_hints = _tie_to_etl(run, analysis)
    payload = {
        **summary,
        "etl_transformations_likely_responsible": etl_hints,
    }
    try:
        # the narrative is a page of prose, not a saga — bound it
        return await astream(
            report_prompt(payload, summary["failures"], project_context),
            max_tokens=800,
            on_delta=on_delta,
            on_thought=on_thought,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("llm report failed, using fallback", error=str(exc))
        return _fallback_narrary(run)
