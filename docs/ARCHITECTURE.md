# Architecture

## Flow

```
  Browser (Next.js chat)
        │  WebSocket /ws/chat  ·  REST /api/*
        ▼
  FastAPI (api:8000)
        │
        ├─ ingest        free text → ConnectionSpec / EtlSubmit
        ├─ intake        source_filter (route) + profile (data metadata)
        ├─ parsers       sqlglot · python ast · dbt manifest
        ├─ connectors    read-only probes per source
        ├─ planner       deterministic baseline + LLM augmentation
        ├─ runner        compiled single-SELECT probes → results
        ├─ browser       approved Playwright checks + screenshot artifacts
        └─ report        structured summary + LLM narrative
        │
        ├──► Postgres (state)        sessions, connections, plans, runs, results
        └──► Redis    (queue + pub/sub) worker channel + progress fan-out
```

## Intake: three destinations, one filter

`app/services/source_filter.py` is the single arbiter for both a git clone
(`repo_fetch.collect`) and a folder upload (`routers/etl.py`), so the two paths
can never disagree about what counts as ETL source:

- **CODE / CONFIG** — parsed into lineage. Content sniffs drop minified files
  and `.sql` files that are really bulk `INSERT` dumps.
- **DATA** — `.csv`, `.tsv`, `.jsonl`, `.parquet` and JSON arrays go to
  `app/services/profile.py`, which returns columns, inferred types, null
  ratios, distinct counts and an exact row count. The rows never enter the
  analysis, the blob or the prompt; only the profile does. A dbt seed is
  registered by name (it *is* a physical table) and profiled for its columns.
- **SKIP** — VCS/dependency directories, binaries, lock files, `.txt`, dbt
  `target/` artifacts other than the manifest, anything over the caps. Every
  drop is counted and summarised back to the user.

Uploads carry `mode`: a directory pick (`folder`) is filtered strictly, while
individual files (`files`) are taken at face value, because naming a file is an
explicit act.

When a profiled file's table name later binds to a connected table, the planner
adds a **contract check** (`CheckKind.SCHEMA` with `expected_columns`): a column
the file promises but the warehouse lacks is drift the pipeline can hide until
the file is re-uploaded.

## Human-in-the-loop

The agent never runs queries on live data until a human approves the plan.
`POST /api/runs/plan` returns a plan in `awaiting_approval`; execution only
begins on `POST /api/runs/{id}/approve` (or the chat "Run N checks" button).

## The three layers of the planner

1. **Deterministic** (`app/agent/planner.py`) — always runs, no LLM required.
   Derives checks from: bound source tables, primary keys, column-name
   heuristics (email/amount/status/rate), data types, timestamp columns, and
   row-count parity between each source and its target. Profiled null rates
   become the thresholds, so budgets reflect reality rather than guesses.
2. **LLM augmentation** (`app/agent/plan_builder.py`) — merges extra checks the
   deterministic layer can't infer (business rules, semantic oddities).
   Every LLM check is coerced through `CheckSpec` validation and de-duplicated;
   malformed output is dropped, never fatal.
3. **Ordering** — checks are sorted by severity so the plan reads top-down by
   blast radius.

## Safety model

- Every data probe is compiled to a **single SELECT** and re-validated by
  `assert_readonly()` immediately before execution.
- Database connections run read-only; passwords are `SecretStr` and
  `redacted()` is the only path back to clients.
- Errors are truncated and typed; browser origins are constrained and metadata
  endpoints are blocked.

## Frontend UI checks

Web targets are stored as `SourceType.WEB` connections, but do not implement
the SQL `Connector` interface. The runner partitions approved plans between the
SQL executor and a Playwright Chromium runner. A same-origin breadth-first link
walk discovers routes and selector hints; deterministic checks cover page loads,
element visibility, console/network health, screenshots, and navigation flows.
LLM checks are constrained to discovered routes/selectors and may add DOM
assertions and UI-to-warehouse parity.
Browser navigation is same-origin by default, with cloud metadata endpoints
blocked. Screenshots and event logs are saved under the shared `ui-artifacts`
volume and served through the run artifact endpoint. Flow actions appear in the
approval plan because they can change application state.


## Failure isolation

A check that errors (bad SQL, permission denied, missing table) is marked
`error` and contributes to the score as a non-pass — it never aborts the run.
Sample-row capture is best-effort and capped by `RUN_MAX_ROW_SAMPLE`.

## Streaming and the thinking feed

Long turns stream through one ordered `EventBuffer`: `thought {text}` step
lines ("Profiling raw.orders…") narrate the deterministic work, while the
LLM's own reply arrives as `message.start` → `message.delta` → `message`
(final frame reuses the streamed id, so the UI replaces its bubble in place
instead of duplicating it).

Thinking-capable models expose their private reasoning live. `core/llm.astream`
sits on the raw OpenAI SDK client (LangChain's chunk rewriting drops
`delta.reasoning`) and routes `delta.reasoning` / `delta.reasoning_content` to
`on_thought`; inline `<think>…</think>` tags are split out of `delta.content` for
providers that stream reasoning in-band. `on_thought` feeds a `_ThinkingBurst`
in the chat router, which emits `thought.open` → `thought.delta` →
`thought.close {secs}` — an expandable "Thinking…" panel that collapses to a
"Thought for Ns" pill once the reply lands. Bursts are live-only: the reasoning
is never persisted, and a reload replays only the replies.

Because reasoning can run long, `astream` bounds it: a chunk that never arrives
within `LLM_STREAM_STALL_SECONDS` (120) or a call that reaches
`LLM_TIMEOUT_SECONDS` ends the stream with whatever was generated, and the
caller falls back (deterministic plan, rule-based narrative). Structured JSON
callers (connection extraction, repair, check explanations) pass
`thinking_disabled()`, which asks Ollama for `reasoning_effort: none` so the
budget goes to the answer instead of a monologue.

## Data policy (hard rule)

No demo data. No dummy data. Only real.

Every audit reads from connections the user actually provided, and every number
in chat, plans and reports comes from a query that really ran. The product never
fabricates or substitutes stand-ins:

- no bundled/demo datasets, seed SQL or synthesized tables pretending to be
  user data;
- no synthetic rows, fake profiles or placeholder values shown to the user;
- no invented results when a check cannot run — it reports `error` honestly.

## Adding a connector

1. Subclass `Connector` in `app/connectors/`.
2. Register it in `_REGISTRY` in `app/connectors/factory.py`.
3. Add the dialect dependency as an extra in `api/pyproject.toml`.

Nothing else changes — planning, execution and the UI are source-agnostic.
