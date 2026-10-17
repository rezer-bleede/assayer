# Architecture

## Flow

```
  Browser (Next.js chat)
        │  WebSocket /ws/chat  ·  REST /api/*
        ▼
  FastAPI (api:8000)
        │
        ├─ ingest        free text → ConnectionSpec / EtlSubmit
        ├─ parsers       sqlglot · python ast · dbt manifest
        ├─ connectors    read-only probes per source
        ├─ planner       deterministic baseline + LLM augmentation
        ├─ runner        compiled single-SELECT probes → results
        └─ report        structured summary + LLM narrative
        │
        ├──► Postgres (state)        sessions, connections, plans, runs, results
        └──► Redis    (queue + pub/sub) worker channel + progress fan-out
```

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

- Every probe is compiled to a **single SELECT** and re-validated by
  `assert_readonly()` (SELECT/WITH/SHOW/DESCRIBE/PRAGMA only, no semicolon
  chaining, keyword blacklist) immediately before execution.
- Connections run read-only (`default_transaction_read_only` for Postgres,
  `SET SESSION TRANSACTION READ ONLY` for MySQL).
- Passwords are `SecretStr`; `redacted()` is the only path back to the client.
- The API never echoes credentials, and errors are truncated + typed.

## Failure isolation

A check that errors (bad SQL, permission denied, missing table) is marked
`error` and contributes to the score as a non-pass — it never aborts the run.
Sample-row capture is best-effort and capped by `RUN_MAX_ROW_SAMPLE`.

## Adding a connector

1. Subclass `Connector` in `app/connectors/`.
2. Register it in `_REGISTRY` in `app/connectors/factory.py`.
3. Add the dialect dependency as an extra in `api/pyproject.toml`.

Nothing else changes — planning, execution and the UI are source-agnostic.