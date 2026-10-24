"""Prompt templates for the data-quality agent."""

from __future__ import annotations

import json

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
    project_context: str | None = None,
) -> str:
    src_block = "\n".join(
        f"- id={s['id']!r} type={s['type']!r} schema={s.get('schema')!r} table={s['table']!r}"
        + (f" routes={json.dumps(s.get('routes', []), ensure_ascii=True)[:8000]}" if s.get("type") == "web" else "")
        for s in sources
    )
    pc = f"{project_context}\n\n" if project_context else ""
    return f"""{pc}## ETL analysis (deterministic parse)
{etl_summary}

## Available tables in connected sources
{src_block or "- (none introspected yet)"}

Sources with type `web` are browser targets (their `table` field is the base URL), not SQL tables. Their `routes` field contains same-origin routes and discovered selector hints; only propose routes and selectors present in that evidence. You may propose page_load, element_present, element_text, console_clean, network_clean, ui_parity, flow, or visual_diff checks using `url`, `selector`, `expected`, and `steps`. UI parity uses a read-only scalar query against a non-web source. Set `params.rebaseline` to true only when the user explicitly requests replacing an approved visual baseline.

## User intent
{user_intent or "Not stated. Infer intent from the ETL script."}

Design the test plan. Respond with a single JSON object:

{{
  "summary": "one-sentence strategy",
  "checks": [
    {{
      "name": "human readable check name",
      "kind": "schema|null_rate|unique|duplicate|referential|row_count|freshness|min_max|range|pattern|distribution|custom_sql|volume|page_load|element_present|element_text|console_clean|network_clean|ui_parity|flow|visual_diff",
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
      "params": {{"parent_table": "<for referential>", "parent_column": "<for referential>", "parent_schema": "<for referential>", "sla_hours": 24, "query": "read-only scalar SQL for ui_parity", "source_id": "warehouse source id", "tolerance": 0.01, "title_pattern": "regex", "ignore_patterns": ["regex"], "viewport": {{"width":1280,"height":720}}, "settle_ms": 300}},
      "rationale": "why this check matters for this pipeline",
      "url": "route path for UI checks, e.g. /dashboard",
      "selector": "CSS selector for element and parity checks",
      "expected": "expected title/text for UI checks",
      "steps": [{{"action":"click|fill|press|wait|goto|assert_text|assert_present","selector":"CSS selector","value":"text or route"}}]
    }}
  ]
}}

Use `"schema"` (not `schema_`) and `"table"` keys exactly as written above. If a project \
 context is given above, honour its instructions and align checks with its knowledge files \
(these supplement the ETL evidence; they never replace it). Cap the plan at 60 checks."""


def interpret_prompt(
    message: str,
    state_summary: str,
    available_sources: list[dict],
) -> str:
    srcs = (
        "\n".join(f"- id={s['id']} {s['name']} ({s['type']})" for s in available_sources)
        or "- none"
    )
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


def report_prompt(
    run_summary: dict, failures: list[dict], project_context: str | None = None
) -> str:
    pc = f"{project_context}\n\n" if project_context else ""
    return f"""{pc}## Run summary
{run_summary}

## Failed checks
{failures or "none"}

Write a concise audit report for a data engineer:
1. **Verdict** — one line: is the pipeline healthy?
2. **What broke** — grouped by severity, cite table.column and the observed numbers.
3. **Likely root cause** — tie each failure back to the ETL transformation that caused it.
4. **Recommended fix** — concrete next actions, ordered by impact.

No preamble, no disclaimers. Plain markdown, max 250 words. If a project context is given \
above, interpret failures against its instructions and knowledge files."""


# --------------------------------------------------------------------------- #
# connection repair
# --------------------------------------------------------------------------- #

REPAIR_ENVIRONMENT = """Facts about the runtime you must reason with:
- The agent runs inside a Docker container. A database running on the user's own
  machine is NOT reachable as `localhost` from here — `host.docker.internal`
  reaches the Docker Desktop host instead; a sibling container is reachable by
  its compose service name.
- ClickHouse speaks HTTP on 8123 (TLS 8443). Ports 9000-9009/9440 are the
  native TCP protocol, which the HTTP driver cannot use at all.
- Other engines: postgres 5432, mysql 3306, mssql 1433, snowflake 443.
- A `Database/role/catalog ... does not exist` error means the *name* is wrong,
  not the host or port."""


def repair_prompt(
    spec: dict,
    failures: list[dict],
    hints: dict,
    databases: list[str] | None = None,
) -> str:
    """Ask for better connection candidates after the deterministic ones failed.

    Only non-secret fields are ever included: host, port, database, engine and
    the driver error text. Username and password are deliberately absent — the
    model must fix addressing, not credentials.
    """
    tried = (
        "\n".join(
            f"- host={f.get('host')!r} port={f.get('port')!r} database={f.get('database')!r}"
            f" -> {f.get('error')}"
            for f in failures
        )
        or "- (nothing tried yet)"
    )
    db_line = f"\nDatabases that exist on that server: {', '.join(databases)}" if databases else ""
    return f"""A database connection keeps failing. Propose the most likely fixes.

{REPAIR_ENVIRONMENT}

Connection (secrets omitted):
  engine: {spec.get("type")}
  host: {spec.get("host")!r}
  port: {spec.get("port")!r}
  database: {spec.get("database")!r}
  schema: {spec.get("schema")!r}

Evidence from the user's code (may itself be stale): {hints}

Already tried (do not repeat these):
{tried}{db_line}

Respond with a single JSON object, no prose, no fences:

{{"candidates": [{{"host": "...", "port": 1234, "database": "...", "reason": "why"}}]}}

Rules:
- 1 to 3 candidates, most likely first. Omit a field to keep the current value.
- You may change host, port and database only. Never propose a username or password.
- The engine never changes."""


EXPLAIN_CHECK_PROMPT = """A data quality check failed. Explain it to the user in plain \
language with the concrete numbers, the likely cause, and what to do next.

check: {check}
observed: {observed}
expected: {expected}
sample of failing rows: {sample}

Reply in plain text, max 120 words. Do not invent data you were not given."""
