#!/usr/bin/env bash
# Reset the demo dataset to its known-dirty state.
#
#   ./scripts/seed_demo.sh
#
# You do NOT need to run this: the stack applies the demo data itself on
# first boot (see scripts/demo/*.sql, applied by the `demo-seed` compose
# service). Run it only to wipe anything you changed in the demo tables and
# get the pristine, deliberately broken dataset back.
#
# Then in the chat window:
#   postgresql://qualitest:qualitest@host.docker.internal:55432/qualitest
#
# Injected defects (each should be caught by a check):
#   raw.orders.order_id    duplicate id
#   raw.orders.customer_id 99 -> orphan (no parent in raw.customers)
#   raw.orders.amount       one NULL, one negative
#   raw.customers.email     'not-an-email' (fails the email pattern)
#   raw.customers.name      one NULL
#   *_revenue               only ~26h of freshness -> stale-data check fails
#   analytics.raw_*         the jaffle-flavored set: orphan customer, NULL
#                            amount, negative payment
set -euo pipefail

COMPOSE="${COMPOSE:-docker compose}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# psql runs inside the db container and reads its own POSTGRES_* variables, so
# a customized .env is honoured without exporting anything here.
PSQL='psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'

# The demo data is the app's own state store, so it is dropped and rebuilt
# rather than merged: this script is the "give me the exact starting point"
# tool, while the compose service is the safe "create what is missing" one.
$COMPOSE exec -T db sh -c "$PSQL" <<'SQL'
DROP TABLE IF EXISTS analytics.daily_revenue;
DROP TABLE IF EXISTS raw.customers;
DROP TABLE IF EXISTS raw.orders;
DROP TABLE IF EXISTS analytics.raw_customers;
DROP TABLE IF EXISTS analytics.raw_orders;
DROP TABLE IF EXISTS analytics.raw_payments;
SQL

for f in "$ROOT"/scripts/demo/*.sql; do
  echo "seeding $(basename "$f")"
  $COMPOSE exec -T db sh -c "$PSQL -f -" < "$f"
done

cat <<'MSG'

Seeded. Paste this into the chat window:

  postgresql://qualitest:qualitest@host.docker.internal:55432/qualitest

...and tell the agent the target is analytics.daily_revenue. Then paste:

  examples/demo_daily_revenue.sql
MSG
