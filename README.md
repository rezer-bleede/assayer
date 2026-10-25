# assayer

<div align="center">

<img src="web/public/logo.svg" alt="Assayer logo" width="64" />

# assayer

**Agentic Testing & QA Platform for Data, ETL, Web Apps, and UI/UX**

*Automated test suites, data drift detection, ETL column-level lineage, Playwright browser checks, and evidence-based scorecards powered by autonomous AI agents.*

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/docker-ready-blue)](https://docker.com/)
[![Next.js](https://img.shields.io/badge/Next.js-14-black)](https://nextjs.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688)](https://fastapi.tiangolo.com/)
[![LangGraph](https://img.shields.io/badge/LangGraph-0.2-blue)](https://langchain-ai.github.io/langgraph/)
[![GitHub Pages](https://img.shields.io/badge/GitHub%20Pages-Live%20Demo-green)](https://assayer.github.io/assayer/)

---

### 🌐 [Visit Marketing & Interactive Documentation Landing Page](https://assayer.github.io/assayer/)

</div>

## Key Features & Capabilities

- **🤖 Autonomous Agentic Test Planning**: Synthesizes baseline and LLM-augmented test suites across databases, ETL pipelines, and web applications.
- **🔄 ETL Lineage & SQL AST Parsing**: Parses SQL queries (sqlglot), Python AST, and dbt models to map table/column lineage and highlight transformation risks automatically.
- **📊 Data Profiling & Drift Detection**: Non-destructively profiles live databases (PostgreSQL, ClickHouse, Snowflake, BigQuery, MySQL, DuckDB) and CSV/Parquet files to inspect distributions, schema contracts, and freshness SLAs.
- **🌐 Web App & UI/UX Automation**: Headless Playwright Chromium test checks covering route loading, element existence, network/console error monitoring, visual screenshot drift, and stateful flow validations.
- **🎯 Quality Scorecards & AI Root Cause Analysis**: Scores quality from 0–100, captures sample failure rows, and outputs detailed root-cause diagnoses and actionable remediation steps.

---

## Core testing & QA capabilities

<table width="100%">
  <tr>
    <td width="50%" align="center" valign="top">
      <h3>1. ETL Testing & Transformation Analysis</h3>
      <img src="docs/assets/usecase1-etl-lineage.gif" alt="ETL Lineage & Transformation Parsing" width="100%" />
      <p><i>Parse SQL queries, Python AST, & dbt models into table/column lineage and flag transformation risks automatically.</i></p>
    </td>
    <td width="50%" align="center" valign="top">
      <h3>2. Data Validation & Drift Detection</h3>
      <img src="docs/assets/usecase2-data-profiling.gif" alt="Data Source Profiling & Drift Detection" width="100%" />
      <p><i>Profile live databases and seed files (CSV/Parquet) to inspect distributions and verify schema contracts.</i></p>
    </td>
  </tr>
  <tr>
    <td width="50%" align="center" valign="top">
      <h3>3. Test Planning Across Targets</h3>
      <img src="docs/assets/usecase3-test-planning.gif" alt="Automated Test Planning & Approval" width="100%" />
      <p><i>Synthesize baseline & LLM-augmented test suites with human-in-the-loop review before execution.</i></p>
    </td>
    <td width="50%" align="center" valign="top">
      <h3>4. QA Results & Scorecard Reporting</h3>
      <img src="docs/assets/usecase4-audit-reporting.gif" alt="Read-Only Audit & Scorecard Reporting" width="100%" />
      <p><i>Execute non-destructive probes, capture failure row samples, score quality (0–100), and generate AI root-cause analysis.</i></p>
    </td>
  </tr>
</table>

**Web and UI/UX testing:** provide a web-app URL for browser-based checks. The agent can verify route loading, visible elements, console/network errors, UI values against source queries, browser flows, and screenshot drift. Data validation is one of the supported verticals, alongside ETL, web, and UI/UX testing.

---

## Quick start

```bash
# 1. Clone the repo
git clone https://github.com/assayer/assayer.git
cd assayer

# 2. Configure a one-time bootstrap token before exposing the app
cp .env.example .env
python3 -c 'import secrets; print("BOOTSTRAP_TOKEN=" + secrets.token_urlsafe(32)); print("SECRET_KEY=" + secrets.token_urlsafe(48))'
# Put both generated values in the matching settings in .env.

# 3. Start the stack
docker compose up -d --build
```

On first visit, create the administrator using the one-time token in `.env`.
The setup closes after the first account is created, and existing chats and
projects are adopted by that account, and the stack applies a demo dataset. A
local model is optional; without one, Assayer uses its deterministic planner.
Keep `.env` private and set a unique `SECRET_KEY`
before deploying beyond a trusted local environment. Google and OpenID Connect
sign-in are optional; configure their provider credentials in `.env`.

| Service | URL |
|---|---|
| Chat UI | http://localhost:3010 |
| API docs | http://localhost:8010/docs |
| Health | http://localhost:8010/healthz |

> Ports are non-default (55432 / 56379 / 8010 / 3010) so the stack can coexist with other projects. Change them in `.env`.

> Light or dark: the header has a switch. The app follows your OS until you
> pick one, then remembers your choice.

> **Projects** (like ChatGPT / Claude): group test sessions into a project and give
> it custom instructions plus small knowledge files — business rules, test
> expectations, runbooks, or technical docs. The agent reads them in every chat filed into
> that project, when it plans tests, runs them and writes the report.
> Projects are optional; unfiled chats behave exactly as before, and the app
> offers a project when you start a new test session.

> **Accounts and workspaces:** accounts use server-side sessions and separate
> personal/team workspace data. Create team workspaces in the account menu;
> chats are private to their creator, while projects and their knowledge files
> are scoped to the active workspace. Personal instructions and appearance are
> available in Settings. Instance AI-provider and execution limits continue to
> be configured in the server environment so they apply consistently to API
> and worker processes.

### The model

The bundled Ollama container is **disabled by default**, so starting Assayer
does not download a model or reserve memory for an inference container. The app
works without a model using its deterministic planner. For local inference,
install Ollama on the host, pull a model, and use the default
`host.docker.internal:11434` endpoint from `.env.example`. To run Ollama in
Compose instead, use `make up-local-model`; it enables the profile, points the
API at the container, and pulls `qwen2.5-coder:3b` (~1.9 GB) on first use. For
direct Compose commands, set `LLM_BASE_URL=http://ollama:11434/v1` and enable
`--profile local-model`.

The local development setup uses `qwen2.5-coder:3b` to keep the model footprint
manageable. The heavier `qwen3.5:9b` and `qwen3.5:9b-8k` tags were removed from
the development Mac; install a larger model only if you specifically want its
reasoning output (see below).

To use a bigger model, or a hosted one:

```dotenv
# in .env
LLM_MODEL=qwen2.5-coder:14b
OLLAMA_MODEL=qwen2.5-coder:14b   # keep the two in sync
```

```dotenv
# or a hosted provider
LLM_PROVIDER=openai
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=sk-...
LLM_MODEL=gpt-4o-mini
```

A free tier without a credit card (Groq, 30 req/min, 1,000 req/day, 200K
tokens/day per model — keys start with `gsk_`):

```dotenv
# in .env
LLM_PROVIDER=groq
LLM_BASE_URL=https://api.groq.com/openai/v1
LLM_API_KEY=gsk_...
LLM_MODEL=openai/gpt-oss-120b
```

#### Run Ollama on the host

Host Ollama keeps inference outside Docker and can use hardware acceleration
available on the host. On macOS it uses Metal; Docker cannot pass through the
Apple GPU to a bundled container. Install Ollama and pull the default model once:

```sh
brew install --cask ollama        # or download from https://ollama.com
ollama pull qwen2.5-coder:3b
make up                           # does not start the bundled runner
```

```dotenv
# in .env
LLM_BASE_URL=http://host.docker.internal:11434/v1
LLM_TIMEOUT_SECONDS=180
```

#### Showing the model's thinking

The chat streams the model's own reasoning above its reply: an expanded
"Thinking…" panel while it reasons, which collapses to "Thought for Ns" when
the answer lands. That needs a thinking-capable model in the same slot —
`qwen2.5-coder:3b` never emits reasoning, so the panel simply never appears.
If you want to enable this optional feature, install a larger model such as
`qwen3.5:9b` (~7.6 GB) with the native Ollama setup:

```dotenv
# in .env
LLM_MODEL=qwen3.5:9b
OLLAMA_MODEL=qwen3.5:9b          # keep the two in sync
LLM_TEMPERATURE=0.6              # its recommended reasoning temperature
LLM_TIMEOUT_SECONDS=600          # thinking runs long; let it finish
```

Reasoning needs context room: the runner's default window (4096) can fill with
reasoning alone, leaving the model looping without ever writing an answer. Bake
a wider window into a derived model (no extra download — it shares the layers):

```sh
printf 'FROM qwen3.5:9b\nPARAMETER num_ctx 8192\n' > /tmp/q.ModelFile
ollama create qwen3.5:9b-8k -f /tmp/q.ModelFile
```

then use `LLM_MODEL=qwen3.5:9b-8k` / `OLLAMA_MODEL=qwen3.5:9b-8k`. The bundled
runner gets `OLLAMA_CONTEXT_LENGTH=16384` from compose. Either way a stalled or
runaway stream is bounded — 120 s of silence or the wall-clock timeout ends the
call with whatever was generated, and the deterministic planner/reporter takes
over.

Env changes apply on `make up` or `docker compose up -d api worker` (a plain
`restart` does not re-read `.env`). To use the bundled runner, run
`make up-local-model` (equivalent to setting
`LLM_BASE_URL=http://ollama:11434/v1` and running
`docker compose --profile local-model up -d --build`). Note `brew install ollama` (without `--cask`)
builds from source and can hang on older macOS — use the cask or the prebuilt
app, and launch it once so its server is listening on `127.0.0.1:11434`.

The LLM is an **enhancement, not a dependency**. Without one — or with the
model still downloading — the deterministic planner and rule-based reporter
still produce a full test plan and a full audit result; only the free-text
narration degrades. A model call is given `LLM_TIMEOUT_SECONDS` (90 by
default) before the fallback takes over, so a slow local model can never
stall the agent for minutes; the plan it then produces is the same one the
deterministic planner would build (verified: 29 checks, same defects).

---

## How it works

```
you share  ──▶  ingest ──▶  analyze target ──▶  inspect evidence ──▶  draft test plan
             (project, URL,  (code, ETL, web,  (sources, routes,     (target-specific
              or source)     UI behavior)       UI behavior)          checks)
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
   An adapter dbt names that we support (e.g. `type: clickhouse`) becomes the
   connection question's pre-filled engine choice.

   A seed CSV that resolves to a live table later adds a **contract check**: any column the file promises but the warehouse lacks is real drift. Uploads can also be sent as `mode=files`, where the user's explicit selection overrides the classification (binaries and hard size caps still apply).

   Connections are pasted as a URL (`postgresql://user:pw@host:5432/db`), a JSON spec, or loose `host=… port=… user=… password=…`. Credentials are stored as secrets and never echoed back.
2. **Parse** — the ETL is analysed deterministically:
   - **SQL** (sqlglot): CTEs, joins, filters, aggregations, projections, casts, unions → table-level lineage + risk flags (`SELECT *`, inner-join row loss, `UNION ALL` duplicates, destructive DDL…).
   - **Python** (AST): `read_sql` / `read_csv` / `to_sql` / `merge` / `groupby` idioms, variable-level lineage, risks (`drop_duplicates`, `fillna`, non-idempotent appends, `dropna` row loss).
   - **dbt**: `manifest.json` lineage + declared tests, or `{{ ref() }}` / `{{ source() }}` from raw model SQL.
3. **Profile** — introspect each source (schemas, tables, columns, types, PKs, row counts) and profile sampled columns.
4. **Plan** — target-specific checks, augmented by the LLM. Data checks use observed profiles and schemas; web/UI checks use discovered routes, elements, and behavior.
5. **Approve** — human-in-the-loop. No checks run against connected sources until you approve the plan.
6. **Execute** — each check compiles to a single aggregate `SELECT`, re-validated as read-only immediately before running.
7. **Report** — QA score (0–100) broken down by severity, check results and available evidence, plus a narrative explaining failures and likely causes.

   Web targets use headless Playwright Chromium. Their approved plans include discovered same-origin routes, element presence, console/network health, screenshot baselines, and navigation flows. Add a target by choosing **Test a web app** or pasting an app URL (for example `http://host.docker.internal:3000`) into chat. The web-target form supports HTTP Basic auth, custom headers, selector-configured form login, and an explicit allowlist for cross-origin assets/API requests; top-level flows remain on the app origin. Credentials are sent to the API rather than through chat. UI-to-warehouse parity checks can be added with a selector and read-only scalar query. Flow checks may interact with application state; review the plan before approving.

---

## Supported data sources

| Type | Notes |
| --- | --- |
| PostgreSQL | read-only transactions |
| MySQL / MariaDB | read-only sessions |
| MSSQL | via ODBC driver 18 |
| ClickHouse | HTTP driver (`clickhouse://…:8123`), session forced to `readonly=2`; introspects `system.*`; `?secure=true` for TLS |
| SQLite / DuckDB files | local paths |
| Snowflake | account / warehouse / role |
| BigQuery | service-account JSON |
| Databricks | SQL warehouse via `http_path` + token |
| S3 / local CSV, JSON, Parquet | DuckDB-backed, treated as tables |
| Web applications | Playwright Chromium: route load, visible elements, console/network errors, UI-vs-query values, flows, visual baselines |

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
    routers/             agent (AG-UI SSE), threads (OpenUI restStorage),
                         projects (grouping + knowledge), chat (legacy WS),
                         connections, etl, runs
    store/               SQLAlchemy state store + repository
    worker.py            Redis-backed execution worker
  tests/                 pytest suite
web/                     Next.js 14 chat UI (standalone build)
docs/                    ARCHITECTURE.md · TEST_STRATEGY.md · index.html
examples/                demo SQL / Python / dbt manifest
scripts/                 seed_demo.sh (optional reset) · demo/*.sql (the seeded data)
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

The **web UI** is OpenUI's `AgentInterface`. It does not post to the legacy
WebSocket; it uses two small protocols:

- **`POST /agent`** — AG-UI SSE. The body is
  `{threadId, messages, tools, context}`; the response is a sequence of
  `TEXT_MESSAGE_START`, one `TEXT_MESSAGE_CONTENT` (an OpenUI Lang program that
  describes the whole turn), and `TEXT_MESSAGE_END`. The model authors the
  program from the turn state; a deterministic serializer is the fallback when
  the model is slow, unavailable, or emits invalid OpenUI Lang.
- **`/api/threads/*`** — thread history for the UI (`GET /get`,
  `POST /create`, `GET /get/{id}`, `PATCH /update/{id}`,
  `DELETE /delete/{id}`).
- **`/api/projects/*`** — Projects (ChatGPT/Claude-style grouping):
  `GET/POST /api/projects`, `PATCH/DELETE /api/projects/{id}`,
  `GET/POST /api/projects/{id}/files` (small text knowledge files),
  `DELETE /api/projects/{id}/files/{file_id}`, plus `POST /api/threads/move`
  to file or unfile a chat. `POST /api/threads/create` accepts a `projectId`
  and `GET /api/threads/get` returns it; a filed chat's instructions and
  knowledge files are folded into the agent's prompts for every turn.

### GitHub integration (initial connector)

Assayer can connect a user's GitHub account, enable repositories for a project,
use explicitly referenced pull requests as cited planning context, and publish
run findings as GitHub issues after a preview and explicit confirmation. The
GitHub account connection is private to the author in the active workspace;
projects only expose repositories that the connected user explicitly enables.

1. Register a GitHub OAuth App with callback
   `http://localhost:8010/api/integrations/github/callback` (adjust for your
   deployment).
2. Set `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, and
   `GITHUB_REDIRECT_URI` in `.env`, set `API_PUBLIC_URL` to the externally
   reachable API origin for published-run links, then restart the API.
3. Open a project's settings, choose **Connect GitHub**, authorize the
   requested `repo` and `read:user` access, then enable one or more repositories.
4. In a chat filed to that project, include an authorized PR URL to use its
   title, description, and changed-file patches as source context.
5. After a run, select an enabled repository, preview/edit the issue, and
   confirm before Assayer creates it.

OAuth tokens are encrypted in the state database using a key derived from
`SECRET_KEY`. Rotating `SECRET_KEY` invalidates stored integration credentials;
users can reconnect the provider. The OAuth App's `repo` scope grants broad
private-repository access; use a GitHub App with narrower repository and issue
permissions for production deployments. GitHub App installation auth, Linear, Asana,
remote MCP, and webhook-triggered workflows are planned extensions to the
shared integration runtime.

The legacy `WS /ws/chat` loop still exists for scripted/older clients: send
`{"session_id": "...", "content": "...", "action": "approve"}` and receive
`message`, `plan.ready`, `check.completed`, `run.completed` events.

Regenerate the OpenUI system prompt and component spec after changing
`web/lib/openui/library.tsx`:

```bash
make openui-prompt
```

---

## Try it in 60 seconds

The stack seeds a deliberately dirty dataset by itself on first boot, so there
is nothing to run. Open the chat at http://localhost:3010 and paste:

```
postgresql://assayer:assayer@host.docker.internal:55432/assayer
```

Paste `examples/demo_daily_revenue.sql`, approve the plan. A verified run
produces **29 checks**, a score of **85.31/100**, and finds every seeded defect
(the same plan comes out whether the local model answers or the deterministic
planner takes over):

| Severity | Check | Result |
| --- | --- | --- |
| critical | `analytics.daily_revenue.customer_id` → `raw.customers` | 1 orphan row |
| critical | `raw.orders.order_id` uniqueness | 1 duplicate |
| warning | `raw.customers.email` pattern | 1 invalid |
| critical | `*.created_at` freshness | older than the 24h SLA |
| info | `*.amount >= 0` | 1 negative value |

0 errors — a failing check is reported, never fatal.

> Want the pristine dataset back after poking at it? `./scripts/seed_demo.sh`
> resets the demo tables. You never have to run it to get started.

## Common commands

```bash
make up       # build + start
make up-local-model # also start the bundled Ollama runner
make logs     # tail
make ps       # status
make test     # pytest in the api container
make lint     # ruff + mypy
make clean    # stop and drop volumes
```

---

## Safety

- Data probes are single `SELECT`s, re-validated immediately before execution (`assert_readonly`), and database connections are opened read-only.
- Browser navigation is limited to the approved origin by default. Flow checks can click, fill, or submit forms; those steps are shown in the plan and run only after approval.
- Browser screenshots and event logs are retained in the `ui-artifacts` volume and are available from run reports.
- Credentials are `SecretStr`; `redacted()` is the only serialisation path.
- Sample-row capture is capped (`RUN_MAX_ROW_SAMPLE`) and best-effort.
- A failing check marks itself `error` — it never aborts the run or the others.

You should still point the agent at a **read replica** rather than production.

---

## Configuration

See `.env.example`. Key knobs:

| Variable | Default | Purpose |
| --- | --- | --- |
| `LLM_PROVIDER` / `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | ollama / http://host.docker.internal:11434/v1 / ollama / qwen2.5-coder:3b | host Ollama or any OpenAI-compatible endpoint |
| `OLLAMA_MODEL` | qwen2.5-coder:3b | model pulled when the optional `local-model` Compose profile is enabled |
| `RUN_MAX_CHECKS` | 200 | cap on checks per run |
| `RUN_MAX_ROW_SAMPLE` | 20 | sample rows captured per failure |
| `LLM_TIMEOUT_SECONDS` | 90 | seconds per model call before the deterministic planner takes over |
| `LLM_MAX_RETRIES` | 0 | retries after the first attempt (raise for a flaky hosted endpoint) |
| `CORS_ORIGINS` | `*` | restrict in production |
| `RUN_MAX_CONCURRENCY` | 4 | concurrent checks per run |
| `UI_ENABLED` | true | enable Playwright browser checks |
| `UI_CHECK_TIMEOUT_SECONDS` | 30 | per-action browser timeout |
| `UI_MAX_STEPS` | 25 | maximum steps in an approved browser flow |
| `UI_MAX_PAGES` | 12 | maximum same-origin routes discovered for a plan |
| `UI_ALLOW_PRIVATE_HOSTS` | true | allow app URLs on private networks (metadata IPs remain blocked) |
| `UI_VISUAL_DEFAULT_TOLERANCE` | 0.005 | accepted pixel-difference fraction for visual checks |
| `INTAKE_CODE_MAX_BYTES` | 131072 | per-file cap for code/config that gets parsed |
| `INTAKE_DATA_MAX_BYTES` | 8000000 | per-file cap for data files that get profiled |
| `INTAKE_TOTAL_MAX_BYTES` | 4000000 | total source text kept for lineage |
| `LLM_TEMPERATURE` | 0.1 | LLM temperature |
| `OPENUI_AUTHOR_ENABLED` | true | let the LLM author each OpenUI turn (fallback if it fails) |
| `OPENUI_AUTHOR_MAX_TOKENS` | 1400 | token budget for one authored OpenUI program |

## License

MIT — see [LICENSE](LICENSE).
