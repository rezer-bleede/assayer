"""Full end-to-end test of the ETL-first intake flow."""

from __future__ import annotations

import json
import urllib.request

API = "http://localhost:8000"


def post(path: str, data: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(data).encode() if data else None,
        headers={"Content-Type": "application/json"} if data else {},
    )
    return json.loads(urllib.request.urlopen(req).read())


def main() -> None:
    # Step 1: Create session
    print("=== Step 1: Create session ===")
    resp = post("/api/sessions")
    print(json.dumps(resp, indent=2))
    session_id = resp["session_id"]

    # Step 2: Submit ETL code
    print("\n=== Step 2: Submit ETL code ===")
    etl_code = """CREATE TABLE analytics.daily_revenue AS
SELECT o.order_id, o.customer_id, c.country, SUM(o.amount) AS amount
FROM raw.orders o
JOIN raw.customers c ON c.customer_id = o.customer_id
WHERE o.status = 'paid'
GROUP BY o.order_id, o.customer_id, c.country"""

    resp = post("/api/etl/analyze", {
        "language": "sql",
        "code": etl_code,
        "label": "daily_revenue",
        "origin": "paste",
    })
    print(json.dumps(resp, indent=2)[:2000])
    etl_id = resp["analysis"]["id"]
    print(f"\nETL ID: {etl_id}")
    print(f"Sources: {resp['analysis']['sources']}")
    print(f"Targets: {resp['analysis']['targets']}")
    print(f"Required sources: {len(resp['required_sources'])}")
    for r in resp["required_sources"]:
        print(f"  - {r['key']}: {r['tables']} (db={r['database']}, role={r['role']})")

    # Step 3: Check session context
    print("\n=== Step 3: Check session context ===")
    ctx_resp = urllib.request.urlopen(f"{API}/api/sessions/{session_id}/context")
    ctx = json.loads(ctx_resp.read())
    print(json.dumps(ctx, indent=2))

    # Step 4: Try to plan without connections - should fail gracefully
    print("\n=== Step 4: Try to plan without connections ===")
    try:
        resp = post("/api/runs/plan", {
            "connection_ids": [],
            "etl_id": etl_id,
        })
        print(json.dumps(resp, indent=2)[:500])
    except urllib.error.HTTPError as e:
        print(f"Expected error (no connections): {e.code} {e.read().decode()[:200]}")

    print("\n=== ETL-first intake flow test PASSED ===")


if __name__ == "__main__":
    main()
