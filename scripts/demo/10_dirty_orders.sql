-- Deliberately dirty demo dataset: applied once on a brand-new database,
-- never touched again (see scripts/demo/20_jaffle.sql for the companion
-- jaffle-flavored set). Idempotent by design: every statement is a
-- CREATE-IF-NOT-EXISTS plus an insert-if-empty guard, so re-running the file
-- is a no-op and user data inside these tables is never clobbered.
--
-- Paste this into the chat at http://localhost:3010:
--   postgresql://assayer:assayer@host.docker.internal:55432/assayer
-- then the example from examples/demo_daily_revenue.sql.

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS analytics;

CREATE TABLE IF NOT EXISTS raw.customers (
  customer_id bigint PRIMARY KEY,
  name        text,
  country     char(3),
  email       text
);

INSERT INTO raw.customers (customer_id, name, country, email)
SELECT * FROM (VALUES
  (1, 'Ada', 'USA', 'ada@example.com'),
  (2, 'Linus', 'SWE', 'linus@example.com'),
  (3, 'Grace', 'USA', 'grace@example.com'),
  (4, NULL, 'XXX', 'not-an-email') -- bad email + NULL name
) AS v(customer_id, name, country, email)
WHERE NOT EXISTS (SELECT 1 FROM raw.customers);

CREATE TABLE IF NOT EXISTS raw.orders (
  order_id    bigint,
  customer_id bigint,
  product_id  bigint,
  amount      numeric(10,2),
  status      text,
  created_at  timestamptz
);

INSERT INTO raw.orders (order_id, customer_id, product_id, amount, status, created_at)
SELECT * FROM (VALUES
  (1, 1, 100, 50.00,  'paid',   now() - interval '30 hours'),
  (2, 1, 101, 25.50,  'paid',   now() - interval '29 hours'),
  (3, 2, 100, 10.00,  'paid',   now() - interval '28 hours'),
  (4, 3, 102, NULL,  'paid',   now() - interval '27 hours'), -- NULL amount
  (4, 3, 102, 99.99, 'paid',   now() - interval '26 hours'), -- duplicate order_id
  (5, 99, 103, 5.00,  'paid',   now() - interval '25 hours'), -- orphan customer_id
  (6, 2, 104, -20.00, 'paid',   now() - interval '26 hours'), -- negative amount
  (7, 3, 105, 75.00, 'pending',now() - interval '48 hours')  -- filtered out below
) AS v(order_id, customer_id, product_id, amount, status, created_at)
WHERE NOT EXISTS (SELECT 1 FROM raw.orders);

CREATE TABLE IF NOT EXISTS analytics.daily_revenue (
  order_id       bigint,
  customer_id    bigint,
  country        char(3),
  amount         numeric(10,2),
  last_order_at  timestamptz
);

INSERT INTO analytics.daily_revenue (order_id, customer_id, country, amount, last_order_at)
SELECT * FROM (
  SELECT o.order_id, o.customer_id, c.country,
         SUM(o.amount) AS amount,
         MAX(o.created_at) AS last_order_at
  FROM raw.orders o
  LEFT JOIN raw.customers c ON c.customer_id = o.customer_id
  WHERE o.status = 'paid' AND o.amount IS NOT NULL
  GROUP BY o.order_id, o.customer_id, c.country
) AS revenue
WHERE NOT EXISTS (SELECT 1 FROM analytics.daily_revenue);
