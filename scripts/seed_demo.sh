#!/usr/bin/env bash
# Seed a small, deliberately dirty dataset so you can exercise the agent
# end-to-end before pointing it at anything real.
#
#   ./scripts/seed_demo.sh
#   # then in the chat window:
#   postgresql://qualitest:qualitest@host.docker.internal:55432/qualitest
#
# Injected defects (each should be caught by a check):
#   raw.orders.order_id    duplicate id
#   raw.orders.customer_id 99 -> orphan (no parent in raw.customers)
#   raw.orders.amount       one NULL, one negative
#   raw.customers.email     'not-an-email' (fails the email pattern)
#   raw.customers.name      one NULL
#   *_revenue               only ~27h of freshness -> stale-data check fails
set -euo pipefail

COMPOSE="${COMPOSE:-docker compose}"

$COMPOSE exec -T db psql -U qualitest -d qualitest <<'SQL'
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS analytics;

DROP TABLE IF EXISTS analytics.daily_revenue;
DROP TABLE IF EXISTS raw.customers;
DROP TABLE IF EXISTS raw.orders;

CREATE TABLE raw.customers (
  customer_id bigint PRIMARY KEY,
  name        text,
  country     char(3),
  email       text
);
INSERT INTO raw.customers VALUES
  (1,'Ada','USA','ada@example.com'),
  (2,'Linus','SWE','linus@example.com'),
  (3,'Grace','USA','grace@example.com'),
  (4,NULL,'XXX','not-an-email');           -- bad email + NULL name

CREATE TABLE raw.orders (
  order_id    bigint,
  customer_id bigint,
  product_id  bigint,
  amount      numeric(10,2),
  status      text,
  created_at  timestamptz
);
INSERT INTO raw.orders VALUES
  (1,1,100,  50.00,'paid',   '2026-10-15 10:00:00+00'),
  (2,1,101,  25.50,'paid',   '2026-10-15 11:00:00+00'),
  (3,2,100,  10.00,'paid',   '2026-10-15 12:00:00+00'),
  (4,3,102,  NULL, 'paid',   '2026-10-15 13:00:00+00'),  -- NULL amount
  (4,3,102,  99.99,'paid',   '2026-10-15 13:30:00+00'),  -- duplicate order_id
  (5,99,103,   5.00,'paid',   '2026-10-15 14:00:00+00'),  -- orphan customer_id
  (6,2,104, -20.00,'paid',   '2026-10-15 15:00:00+00'),  -- negative amount
  (7,3,105,  75.00,'pending','2026-10-15 16:00:00+00');  -- filtered out below

CREATE TABLE analytics.daily_revenue AS
SELECT o.order_id, o.customer_id, c.country,
       SUM(o.amount)   AS amount,
       MAX(o.created_at) AS last_order_at
FROM raw.orders o
LEFT JOIN raw.customers c ON c.customer_id = o.customer_id
WHERE o.status = 'paid' AND o.amount IS NOT NULL
GROUP BY o.order_id, o.customer_id, c.country;

SELECT 'raw.orders'            AS t, count(*) FROM raw.orders
UNION ALL SELECT 'raw.customers',       count(*) FROM raw.customers
UNION ALL SELECT 'analytics.daily_revenue', count(*) FROM analytics.daily_revenue;
SQL

cat <<'MSG'

Seeded. Paste this into the chat window:

  postgresql://qualitest:qualitest@host.docker.internal:55432/qualitest

...and tell the agent the target is analytics.daily_revenue. Then paste:

  examples/demo_daily_revenue.sql
MSG