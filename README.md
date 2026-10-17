# data-qualitest-agent

<div align="center">

# qualitest

**Agentic data testing and data quality auditing.**

Provide your data connection details and ETL code/script — the agent figures out transformations, designs a test plan, connects to your data sources, runs tests, and reports results.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/docker-ready-blue)](https://docker.com/)
[![Next.js](https://img.shields.io/badge/Next.js-14-black)](https://nextjs.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688)](https://fastapi.tiangolo.com/)
[![LangGraph](https://img.shields.io/badge/LangGraph-0.2-blue)](https://langchain-ai.github.io/langgraph/)

</div>

## Key Use Cases & Capabilities

<table width="100%">
  <tr>
    <td width="50%" align="center" valign="top">
      <h3>1. ETL Lineage & Transformation Parsing</h3>
      <img src="docs/assets/usecase1-etl-lineage.gif" alt="ETL Lineage & Transformation Parsing" width="100%" />
      <p><i>Parse SQL queries, Python AST, & dbt models into table/column lineage and flag transformation risks automatically.</i></p>
    </td>
    <td width="50%" align="center" valign="top">
      <h3>2. Data Source Profiling & Drift Detection</h3>
      <img src="docs/assets/usecase2-data-profiling.gif" alt="Data Source Profiling & Drift Detection" width="100%" />
      <p><i>Profile live databases and seed files (CSV/Parquet) to inspect distributions and verify schema contracts.</i></p>
    </td>
  </tr>
  <tr>
    <td width="50%" align="center" valign="top">
      <h3>3. Automated Test Planning & Approval</h3>
      <img src="docs/assets/usecase3-test-planning.gif" alt="Automated Test Planning & Approval" width="100%" />
      <p><i>Synthesize baseline & LLM-augmented test suites with human-in-the-loop review before execution.</i></p>
    </td>
    <td width="50%" align="center" valign="top">
      <h3>4. Read-Only Audit & Scorecard Reporting</h3>
      <img src="docs/assets/usecase4-audit-reporting.gif" alt="Read-Only Audit & Scorecard Reporting" width="100%" />
      <p><i>Execute non-destructive probes, capture failure row samples, score quality (0–100), and generate AI root-cause analysis.</i></p>
    </td>
  </tr>
</table>

## Quick start

```bash
# 1. Clone the repo
git clone <your-repo-url>
cd data-qualitest-agent

# 2. Copy env and configure
cp .env.example .env
# Add your LLM key (optional — the app works without one)

# 3. Start the stack
docker compose up -d --build
```

| Service | URL |
|---|---|
| Chat UI | http://localhost:3010 |
| API docs | http://localhost:8010/docs |
| Health | http://localhost:8010/healthz |

> Ports are non-default (5433 / 6380 / 8010 / 3010) so the stack can coexist with other projects. Change them in `.env`.

### With a local model instead of a hosted key

```bash
docker compose --profile ollama up -d
docker compose exec ollama ollama pull qwen2.5-coder:14b
```

Then in `.env`:

```dotenv
LLM_PROVIDER=ollama
LLM_BASE_URL=http://ollama:11434/v1
LLM_API_KEY=ollama
LLM_MODEL=qwen2.5-coder:14b
```

The LLM is an **enhancement, not a dependency**. Without one, the deterministic planner and rule-based reporter still produce a full test plan and a full audit result — only the free-text narration degrades.

---

## How it works

```
you paste  ──▶  ingest ──▶  parse ETL ──▶  profile sources ──▶  draft test plan
              (URLs, creds)  (sqlglot /   (schemas, columns,   (keys, nulls,
                             ast / dbt)    row counts, types)   ranges, parity)
                                                                 │
                                           you approve  ◀────────┘
                                                 │
                                                 ▼
                                         run read-only probes ──▶ scored results + narrative
```

### How it works, step by step

1. **Intake** — a git URL, a pasted script, or a folder of files. Every file is routed before it is read:
   - **code / config** (`.sql`, `.py`, `.ddl`, `.j2`, dbt YAML, `manifest.json`) → parsed for lineage;
   - **data** (`.csv`, `.tsv`, `.jsonl`, `.parquet`, JSON arrays) → *profiled* into columns, types, row counts and sample values. The rows never enter the analysis, so a 40 MB seed costs kilobytes of context;
   - **skipped** — VCS/dependency directories, binaries, lock files (`package-lock.json`, `poetry.lock`…), `.txt`, dbt `target/` artifacts other than the manifest, minified files and `.sql` files that are really bulk `INSERT` dumps.

   dbt's Jinja templates are resolved the same way: `{{ env_var('X', 'default') }}`
   yields its default literal; anything unresolvable (no default, conditionals) is
   dropped rather than shown raw — a hint we cannot honour never becomes a lie in the UI.

   A seed CSV that resolves to a live table later adds a **contract check**: any column the file promises but the warehouse lacks is real drift. Uploads can also be sent as `mode=files`, where the user's explicit selection overrides the classification (binaries and hard size caps still apply).

   Connections are pasted as a URL (`postgresql://user:pw@host:5432/db`), a JSON spec, or loose `host=… port=… user=… password=…`. Credentials are stored as secrets and never echoed back.
2. **Parse** — the ETL is analysed deterministically:
   - **SQL** (sqlglot): CTEs, joins, filters, aggregations, projections, casts, unions → table-level lineage + risk flags (`SELECT *`, inner-join row loss, `UNION ALL` duplicates, destructive DDL…).
   - **Python** (AST): `read_sql` / `read_csv` / `to_sql` / `merge` / `groupby` idioms, variable-level lineage, risks (`drop_duplicates`, `fillna`, non-idempotent appends, `dropna` row loss).
   - **dbt**: `manifest.json` lineage + declared tests, or `{{ ref() }}` / `{{ source() }}` from raw model SQL.
3. **Profile** — introspect each source (schemas, tables, columns, types, PKs, row counts) and profile sampled columns.
4. **Plan** — a deterministic baseline (keys → lineage parity → completeness → validity → freshness → drift), augmented by the LLM. Thresholds are set from *observed* null rates, not guesses.
5. **Approve** — human-in-the-loop. Nothing touches your data until you click.
6. **Execute** — each check compiles to a single aggregate `SELECT`, re-validated as read-only immediately before running.
7. **Report** — weighted quality score (0–100) broken down by severity, pass/fail table, failing-row samples, and a narrative tying failures back to the transformation that caused them.

---

## Supported data sources

| Type | Notes |
| --- | --- |
| PostgreSQL | read-only transactions |
| MySQL / MariaDB | read-only sessions |
| MSSQL | via ODBC driver 18 |
| SQLite / DuckDB files | local paths |
| Snowflake | account / warehouse / role |
| BigQuery | service-account JSON |
| Databricks | SQL warehouse via `http_path` + token |
| S3 / local CSV, JSON, Parquet | DuckDB-backed, treated as tables |

Add another by subclassing `Connector` and registering it in `api/app/connectors/factory.py` — planning, execution and UI are source-agnostic.

---

## Project layout

```
api/                     FastAPI + LangGraph backend
  app/
    agent/               planner, plan_builder, graph, prompts, tools
    connectors/          per-source read-only connectors + factory
    parsers/             sql (sqlglot) · python (ast) · dbt
    runner/              check compilation, executor, scoring
    services/            chat ingest, audit orchestration
    routers/             chat (WS), connections, etl, runs
    store/               SQLAlchemy state store + repository
    worker.py            Redis-backed execution worker
  tests/                 pytest suite
web/                     Next.js 14 chat UI (standalone build)
docs/                    ARCHITECTURE.md · TEST_STRATEGY.md
examples/                demo SQL / Python / dbt manifest
scripts/                 seed_demo.sh, docker-healthcheck.sh
```

---

## REST API

```bash
# 1. register + verify a source
curl -X POST localhost:8010/api/connections/test \
  -H 'content-type: application/json' \
  -d '{"spec":{"type":"postgres","name":"wh","host":"localhost",
               "port":5433,"database":"dwh","username":"me","password":"pw",
               "options":{"schema":"analytics"}}}'

# 2. analyse the ETL
curl -X POST localhost:8010/api/etl/analyze \
  -H 'content-type: application/json' \
  -d '{"language":"sql","code":"INSERT INTO analytics.rev SELECT id FROM raw.orders"}'

# 3. analyse an ETL from a Git repo
curl -X POST http://localhost:8010/api/etl/from-repo \
  -H 'content-type: application/json' \
  -d '{"url":"https://github.com/acme/analytics","label":"my project"}'

# 4. upload a project folder (or individual files)
#    mode=folder -> strict filtering: junk is expected, data files are profiled
#    mode=files  -> the user named them, so they are taken at face value
curl -X POST localhost:8010/api/etl/upload \
  -F "mode=folder" \
  -F "files=@shop/models/staging.sql" \
  -F "files=@shop/seeds/raw_customers.csv" \
  -F 'paths=["shop/models/staging.sql","shop/seeds/raw_customers.csv"]'

# 5. draft a plan (returns run_id, status=awaiting_approval)
curl -X POST localhost:8010/api/runs/plan \
  -H 'content-type: application/json' \
  -d '{"connection_ids":["<conn_id>"],"etl_id":"<etl_id>"}'

# 6. approve + execute
curl -X POST localhost:8010/api/runs/<run_id>/approve \
  -H 'content-type: application/json' \
  -d '{"connection_ids":["<conn_id>"]}'

# 7. results / report / CSV export
curl localhost:8010/api/runs/<run_id>
curl localhost:8010/api/runs/<run_id>/report
curl localhost:8010/api/runs/<run_id>/export
```

`WS /ws/chat` carries the full conversational loop: send
`{"session_id": "...", "content": "...", "action": "approve"}` and receive
`message`, `plan.ready`, `check.completed`, `run.completed` events.

---

## Try it in 60 seconds

```bash
./scripts/seed_demo.sh          # seeds a deliberately dirty dataset
```

Then in the chat at http://localhost:3010:

```
postgresql://qualitest:qualitest@host.docker.internal:55432/qualitest
```

Paste `examples/demo_daily_revenue.sql`, approve the plan. A verified run
produces **29 checks**, a score of **85.31/100**, and finds every seeded defect:

| Severity | Check | Result |
| --- | --- | --- |
| critical | `analytics.daily_revenue.customer_id` → `raw.customers` | 1 orphan row |
| critical | `raw.orders.order_id` uniqueness | 1 duplicate |
| warning | `raw.customers.email` pattern | 1 invalid |
| critical | `*.created_at` freshness | ~28h old vs 24h SLA |
| info | `*.amount >= 0` | 1 negative value |

0 errors — a failing check is reported, never fatal.

## Common commands

```bash
make up       # build + start
make logs     # tail
make ps       # status
make test     # pytest in the api container
make lint     # ruff + mypy
make clean    # stop and drop volumes
```

---

## Safety

- All probes are single `SELECT`s, re-validated immediately before execution (`assert_readonly`), and connections are opened read-only.
- Credentials are `SecretStr`; `redacted()` is the only serialisation path.
- Sample-row capture is capped (`RUN_MAX_ROW_SAMPLE`) and best-effort.
- A failing check marks itself `error` — it never aborts the run or the others.

You should still point the agent at a **read replica** rather than production.

---

## Configuration

See `.env.example`. Key knobs:

| Variable | Default | Purpose |
| --- | --- | --- |
| `LLM_PROVIDER` / `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | openai / — / — / gpt-4o-mini | any OpenAI-compatible endpoint |
| `RUN_MAX_CHECKS` | 200 | cap on checks per run |
| `RUN_MAX_ROW_SAMPLE` | 20 | sample rows captured per failure |
| `CORS_ORIGINS` | `*` | restrict in production |
| `RUN_MAX_CONCURRENCY` | 4 | concurrent checks per run |
| `INTAKE_CODE_MAX_BYTES` | 131072 | per-file cap for code/config that gets parsed |
| `INTAKE_DATA_MAX_BYTES` | 8000000 | per-file cap for data files that get profiled |
| `INTAKE_TOTAL_MAX_BYTES` | 4000000 | total source text kept for lineage |
| `LLM_TEMPERATURE` | 0.1 | LLM temperature |

## License

MIT — see [LICENSE](LICENSE).
