"""Prompt templates for the data-quality agent."""

from __future__ import annotations

SYSTEM = """You are the test-engineering copilot inside `qualitest`, an agentic data \
quality auditing tool.

Your job: given data source metadata and a user's ETL script, derive what the \
transformation actually does, then design a focused, high-signal data test plan.

Operating rules:
1. Ground every check in evidence: a schema column, a join key, a filter, or an \
ETL expression. Never invent tables, columns or joins that are not present.
2. Prioritise by blast radius: keys and join columns first, then completeness, \
then value validity, then distribution.
3. Be specific with thresholds. Prefer profiling-derived numbers (e.g. observed \
null rate 2.1%) over generic defaults, and set sensible severities \
(blocker > critical > warning > info).
4. Keep the plan tight. Fewer meaningful checks beat a long checklist.
5. Never emit credentials, connection strings or secrets in your answers.

When you respond with JSON, respond with JSON only — no prose, no markdown fences."""


def plan_prompt(
    etl_summary: str,
    sources: list[dict],
    user_intent: str | None = None,
) -> str:
    src_block = "\n".join(
        f"- id={s['id']!r} type={s['type']!r} schema={s.get('schema')!r} table={s['table']!r}"
        for s in sources
    )
    return f"""## ETL analysis (deterministic parse)
{etl_summary}

## Available tables in connected sources
{src_block or "- (none introspected yet)"}

## User intent
{user_intent or "Not stated. Infer intent from the ETL script."}

Design the test plan. Respond with a single JSON object:

{{
  "summary": "one-sentence strategy",
  "checks": [
    {{
      "name": "human readable check name",
      "kind": "schema|null_rate|unique|duplicate|referential|row_count|freshness|min_max|range|pattern|distribution|custom_sql|volume",
      "source_id": "<id of the connected source>",
      "schema": "<database schema or null>",
      "table": "<table name exactly as in the source>",
      "column": "<column or null>",
      "severity": "blocker|critical|warning|info",
      "threshold": <number for null_rate (max null fraction) or referential (max orphans)>,
      "tolerance_ratio": <number for row_count/volume, e.g. 0.02>,
      "min_value": <number/string for range/min_max, else null>,
      "max_value": <number/string for range/min_max, else null>,
      "regex": "<pattern for pattern checks, else null>",
      "params": {{"parent_table": "<for referential>", "parent_column": "<for referential>", "parent_schema": "<for referential>", "sla_hours": 24}},
      "rationale": "why this check matters for this pipeline"
    }}
  ]
}}

Use `"schema"` (not `schema_`) and `"table"` keys exactly as written above. Cap the plan at 60 checks."""


def interpret_prompt(
    message: str,
    state_summary: str,
    available_sources: list[dict],
) -> str:
    srcs = "\n".join(f"- id={s['id']} {s['name']} ({s['type']})" for s in available_sources) or "- none"
    return f"""## Conversation state
{state_summary}

## Connected sources
{srcs}

## New user message
{message}

Decide the single next best action and reply in plain conversational language for a \
chat UI (max 120 words). If the user supplied a connection string, password or ETL \
code, acknowledge what was captured and state what you need next. If a test plan is \
awaiting approval, remind them they can approve, edit or regenerate it."""


def report_prompt(run_summary: dict, failures: list[dict]) -> str:
    return f"""## Run summary
{run_summary}

## Failed checks
{failures or "none"}

Write a concise audit report for a data engineer:
1. **Verdict** — one line: is the pipeline healthy?
2. **What broke** — grouped by severity, cite table.column and the observed numbers.
3. **Likely root cause** — tie each failure back to the ETL transformation that caused it.
4. **Recommended fix** — concrete next actions, ordered by impact.

No preamble, no disclaimers. Plain markdown, max 250 words."""


EXPLAIN_CHECK_PROMPT = """A data quality check failed. Explain it to the user in plain \
language with the concrete numbers, the likely cause, and what to do next.

check: {check}
observed: {observed}
expected: {expected}
sample of failing rows: {sample}

Reply in plain text, max 120 words. Do not invent data you were not given."""