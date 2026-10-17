# data-qualitest-agent

Agentic data testing and data quality auditing.

You give it **data connection details** and **an ETL script**. It figures out the
transformations, designs a test plan, connects to your sources, runs the tests,
and reports what broke — with row-level samples.

Everything runs in Docker.

---

## Quick start

```bash
cp .env.example .env       # add your LLM key (optional — the app works without one)
make up                    # or: docker compose up -d --build
```

| | |
| --- | --- |
| Chat UI | http://localhost:3010 |
| API docs | http://localhost:8010/docs |
| Health | http://localhost:8010/healthz |

> Ports are non-default (5433 / 6380 / 8010 / 3010) so the stack can coexist with
> other projects. Change them in `.env`.

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

The LLM is an **enhancement, not a dependency**. Without one, the deterministic
planner and rule-based reporter still produce a full test plan and a full audit
result — only the free-text narration degrades.

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

1. **Intake** — paste a connection URL (`postgresql://user:pw@host:5432/db`), a JSON
   spec, or loose `host=… port=… user=… password=…`. Credentials are stored as
   secrets and never echoed back.
2. **Parse** — the ETL is analysed deterministically:
   - **SQL** (sqlglot): CTEs, joins, filters, aggregations, projections, casts,
     unions → table-level lineage + risk flags (`SELECT *`, inner-join row loss,
     `UNION ALL` duplicates, destructive DDL…).
   - **Python** (AST): `read_sql` / `read_csv` / `to_sql` / `merge` / `groupby`
     idioms, variable-level lineage, risks (`drop_duplicates`, `fillna`,
     non-idempotent appends, `dropna` row loss).
   - **dbt**: `manifest.json` lineage + declared tests, or `{{ ref() }}` /
     `{{ source() }}` from raw model SQL.
3. **Profile** — introspect each source (schemas, tables, columns, types, PKs,
   row counts) and profile sampled columns.
4. **Plan** — a deterministic baseline (keys → lineage parity → completeness →
   validity → freshness → drift), augmented by the LLM. Thresholds are set from
   *observed* null rates, not guesses.
5. **Approve** — human-in-the-loop. Nothing touches your data until you click.
6. **Execute** — each check compiles to a single aggregate `SELECT`, re-validated
   as read-only immediately before running.
7. **Report** — weighted quality score (0–100) broken down by severity, pass/fail
   table, failing-row samples, and a narrative tying failures back to the
   transformation that caused them.

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

Add another by subclassing `Connector` and registering it in
`app/connectors/factory.py` — planning, execution and UI are source-agnostic.

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

# 3. draft a plan (returns run_id, status=awaiting_approval)
curl -X POST localhost:8010/api/runs/plan \
  -H 'content-type: application/json' \
  -d '{"connection_ids":["<conn_id>"],"etl_id":"<etl_id>"}'

# 4. approve + execute
curl -X POST localhost:8010/api/runs/<run_id>/approve \
  -H 'content-type: application/json' \
  -d '{"connection_ids":["<conn_id>"]}'

# 5. results / report / CSV export
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

- All probes are single `SELECT`s, re-validated immediately before execution
  (`assert_readonly`), and connections are opened read-only.
- Credentials are `SecretStr`; `redacted()` is the only serialisation path.
- Sample-row capture is capped (`RUN_MAX_ROW_SAMPLE`) and best-effort.
- A failing check marks itself `error` — it never aborts the run or the others.

You should still point the agent at a **read replica** rather than production.

## Configuration

See `.env.example`. Key knobs:

| Variable | Default | Purpose |
| --- | --- | --- |
| `LLM_PROVIDER` / `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | openai / — / — / gpt-4o-mini | any OpenAI-compatible endpoint |
| `RUN_MAX_CHECKS` | 200 | cap on checks per run |
| `RUN_MAX_ROW_SAMPLE` | 20 | sample rows captured per failure |
| `CORS_ORIGINS` | `*` | restrict in production |

## License

MIT — see [LICENSE](LICENSE).