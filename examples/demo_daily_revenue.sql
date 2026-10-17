-- Demo ETL: SQL
-- Paste this in the chat window after providing your connection URL.

CREATE TABLE analytics.daily_revenue AS
SELECT
    o.order_id,
    o.customer_id,
    c.country,
    SUM(o.amount)                          AS amount,
    COUNT(DISTINCT o.product_id)           AS products,
    MAX(o.created_at)                      AS last_order_at
FROM raw.orders o
LEFT JOIN raw.customers c
       ON c.customer_id = o.customer_id
WHERE o.status = 'paid'
  AND o.amount IS NOT NULL
GROUP BY o.order_id, o.customer_id, c.country;