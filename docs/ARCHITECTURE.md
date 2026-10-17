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