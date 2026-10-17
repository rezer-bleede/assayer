#!/bin/bash
set -e

API="http://localhost:8000"

echo "=== Step 1: Create session ==="
SESSION=$(curl -s -X POST "$API/api/sessions" -H "Content-Type: application/json" -d '{}' | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
echo "Session: $SESSION"

echo ""
echo "=== Step 2: Submit ETL code ==="
ETL="CREATE TABLE analytics.daily_revenue AS SELECT o.order_id, o.customer_id, c.country, SUM(o.amount) AS amount FROM raw.orders o JOIN raw.customers c ON c.customer_id = o.customer_id WHERE o.status = 'paid' GROUP BY o.order_id, o.customer_id, c.country"
RESP=$(curl -s -X POST "$API/api/etl/analyze" -H "Content-Type: application/json" -d "{\"language\":\"sql\",\"code\":\"$(echo "$ETL" | sed 's/"/\\"/g')\",\"label\":\"daily_revenue\",\"origin\":\"paste\"}")
ETL_ID=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['analysis']['id'])")
echo "ETL ID: $ETL_ID"
echo "Required sources:"
echo "$RESP" | python3 -c "import sys,json; [print(f'  - {r[\"key\"]}: {r[\"tables\"]} (db={r[\"database\"]}, role={r[\"role\"]})') for r in json.load(sys.stdin)['required_sources']]"

echo ""
echo "=== Step 3: Provide connection for raw database ==="
RESP=$(curl -s -X POST "$API/api/connections/from-url" -H "Content-Type: application/json" -d "{\"url\":\"postgresql://qualitest:qualitest@db:5432/qualitest\",\"session_id\":\"$SESSION\",\"requirement_key\":\"db:raw\",\"tables\":[\"raw.orders\",\"raw.customers\"],\"target_tables\":[]}")
echo "$RESP" | python3 -m json.tool | head -10
CONN_ID=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['connection_id'])")
echo "Connection ID: $CONN_ID"

echo ""
echo "=== Step 4: Create plan ==="
RESP=$(curl -s -X POST "$API/api/runs/plan" -H "Content-Type: application/json" -d "{\"connection_ids\":[\"$CONN_ID\"],\"etl_id\":\"$ETL_ID\"}")
echo "$RESP" | python3 -m json.tool | head -20
PLAN_ID=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['run_id'])")
echo "Plan ID: $PLAN_ID"

echo ""
echo "=== Step 5: Approve and run ==="
RESP=$(curl -s -X POST "$API/api/runs/$PLAN_ID/approve" -H "Content-Type: application/json" -d "{\"connection_ids\":[\"$CONN_ID\"]}")
echo "$RESP" | python3 -m json.tool | head -30
RUN_ID=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['run']['id'])")
echo "Run ID: $RUN_ID"

echo ""
echo "=== Step 6: Get run results ==="
sleep 3
curl -s "$API/api/runs/$RUN_ID" | python3 -m json.tool | head -40

echo ""
echo "=== E2E FLOW TEST PASSED ==="