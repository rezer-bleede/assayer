# Demo ETL: Python (pandas)
# Paste this in the chat window after providing your connection URL.

import pandas as pd

orders = pd.read_sql(
    "SELECT * FROM raw.orders WHERE created_at >= '2026-01-01'", engine
)
customers = pd.read_sql("SELECT * FROM raw.customers", engine)

merged = orders.merge(
    customers[["customer_id", "country"]], on="customer_id", how="left"
)

totals = (
    merged.groupby(["customer_id", "country"], dropna=False)
    .agg(amount=("amount", "sum"), orders=("order_id", "nunique"))
    .reset_index()
)

totals.to_sql(
    "customer_totals",
    engine,
    if_exists="append",   # agent will flag this as non-idempotent
    index=False,
)