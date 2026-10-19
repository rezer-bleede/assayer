# Test strategy

## What the agent checks

| Category | Checks | Typical severity |
| --- | --- | --- |
| Shape | table readable, not-empty | blocker |
| Keys | PK uniqueness, key-like column uniqueness | blocker / critical |
| Lineage | row-count parity source → target | critical |
| Referential | FK orphans against the source table | critical |
| Completeness | per-column null rate vs profiled budget | critical (keys) / warning |
| Validity | email pattern, ISO currency, 0–1 rates, non-negative amounts, numeric ranges | warning / info |
| Freshness | max(timestamp) within SLA (default 24h) | critical on targets |
| Drift | cardinality snapshots for low-cardinality categoricals | info |
| Custom | user-asserted boolean SQL | any |

## Threshold philosophy

Thresholds come from **observed data**, not convention. When a table is profiled
before planning, the null-rate budget is set to the null rate measured at plan
time — which catches *increases* and drift. Without profiling it falls back to a
generic 5%. Row-count parity uses a two-tier tolerance: 5% for small drift,
35% when the initial comparison is already large, to avoid noisy failures on
known lossy pipelines (inner joins, filters).

## Scoring

Weighted pass rate, not a raw count, so one blocker outweighs ten info checks.

```
score = 100 * Σ(weight of passed|skipped) / Σ(weight)
weights: blocker 4.0 · critical 2.0 · warning 1.0 · info 0.25
```

The score is also broken down per severity so the UI can show *where* quality
degraded, not just an opaque number.

## Backend test suite

```bash
make test          # pytest inside the api container
```

- `test_safety.py` — the read-only SQL guard rejects mutations and multi-statements.
- `test_ingest.py` — URL/credential/JSON extraction, redaction, language detection.
- `test_parsers.py` — SQL lineage, CTE/join/filter/agg detection, risk flags;
  Python AST sinks and sources; dbt manifest + `ref()`/`source()` extraction.
- `test_planner.py` — baseline coverage, dedup, determinism, parity checks, and
  the file-to-warehouse contract check driven by data profiles.
- `test_connectors_clickhouse.py` — ClickHouse dialect: `system.*` introspection SQL,
  `readonly=2` session settings, the `NOT match()` regex form (with doubled
  backslashes — ClickHouse eats string escapes), and the Postgres default rendering
  staying byte-for-byte identical.
- `test_source_filter.py` — intake routing shared by git and upload: what is
  code, what is data, what is junk, plus the generated/`.sql`-dump sniffs, the
  aggregate budget and `mode=files` trust.
- `test_profile.py` — data-file profiling: CSV/TSV/JSON/NDJSON types, null
  ratios, distinct counts, exact row counts beyond the sample window, parquet
  via duckdb, and truncated scans being flagged rather than hidden.
- `test_intake_upload.py` — the upload route end to end: routing, profiling,
  the note the user sees, and trust mode.
- `test_runner.py` — end-to-end run against an in-memory fake connector:
  correct pass/fail classification, scoring, event emission, unsafe-SQL rejection.

## Intake policy (upload and git clone share it)

Each file is routed before its bytes are used:

| Route | Formats | What happens |
| --- | --- | --- |
| code / config | `.sql` `.py` `.ddl` `.j2` `.jinja` `.yml` `.yaml`, small config JSON, `target/manifest.json` | parsed into lineage (cap `INTAKE_CODE_MAX_BYTES`) |
| data | `.csv` `.tsv` `.jsonl` `.parquet`, JSON arrays | profiled: columns, inferred types, null ratio, distinct counts, exact row count. **Rows never enter the analysis.** |
| skip | VCS/dependency dirs, binaries, lock files, `.txt`, `target/` artifacts, minified files, `.sql` bulk-INSERT dumps | counted in the note the user sees |

`mode=files` (the user named individual files) skips the classification:
everything textual is read, and only binaries and hard size caps still apply.

## Verifying manually

```bash
docker compose up -d --build
# UI      http://localhost:3010
# API docs http://localhost:8010/docs
```

The demo dataset is applied by the stack itself on first boot
(`scripts/demo/*.sql`, via the `demo-seed` service) — there is nothing to seed
by hand.

Then paste `postgresql://qualitest:qualitest@host.docker.internal:55432/qualitest`
and `examples/demo_daily_revenue.sql` into the chat. Verified baseline:
**29 checks, 85.31/100, 22 passed, 7 failed, 0 errors.**

## Regression tests worth knowing about

These were real bugs found during integration, each now pinned by a test:

- `test_store.py` — `with SessionLocal()` closes *without* committing; every
  write was silently discarded.
- `test_schemas.py` — `SecretStr.model_dump(mode="json")` yields `"**********"`,
  so persisted credentials could never authenticate. Round-trip is now asserted.
- `test_compiler.py` — an open-ended range rendered `> None` and errored.
- `test_parsers.py` — the regex fallback returned empty sources/targets.
- `test_chat_events.py` — fire-and-forget event tasks reordered the stream.
- `test_source_filter.py` — a seed CSV was read whole and a `package-lock.json`
  was parsed as source; data files are now profiled and lock files dropped.