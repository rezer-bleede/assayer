-- JaFFLe-flavored demo dataset: required by scripts/flow_check.py and by the
-- README screenshots. Applied once on a brand-new database, never touched
-- again. Idempotent by design: CREATE-IF-NOT-EXISTS plus ON CONFLICT DO
-- NOTHING inserts, so re-running is a no-op.

CREATE SCHEMA IF NOT EXISTS analytics;

CREATE TABLE IF NOT EXISTS analytics.raw_customers (
  id         integer PRIMARY KEY,
  first_name text,
  last_name  text
);

INSERT INTO analytics.raw_customers (id, first_name, last_name)
VALUES (1, 'A', 'B'), (2, 'C', NULL), (9, 'D', 'E')
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS analytics.raw_orders (
  id           integer PRIMARY KEY,
  customer_id  integer,
  order_date   date,
  status       text,
  amount_total numeric(12,2)
);

INSERT INTO analytics.raw_orders (id, customer_id, order_date, status, amount_total)
VALUES
  (1, 1, '2026-01-01', 'shipped',   120.00),
  (2, 2, '2026-01-02', 'returned',  10.00),
  (3, 1, '2026-01-03', 'completed', 55.50),
  (4, 3, '2026-01-04', 'shipped',   NULL) -- orphan customer_id, NULL amount
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS analytics.raw_payments (
  id            integer PRIMARY KEY,
  order_id      integer,
  payment_method text,
  amount        numeric(12,2)
);

INSERT INTO analytics.raw_payments (id, order_id, payment_method, amount)
VALUES
  (1, 1, 'card',   120.00),
  (2, 2, 'bank',    -5.00), -- negative payment
  (3, 2, 'cash',     7.00)
ON CONFLICT (id) DO NOTHING;
