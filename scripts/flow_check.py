"""Simulate the browser: REST for credentials, WebSocket for the agent's questions.

Run inside the api container. The demo tables this needs are applied by the
stack itself on first boot (`scripts/demo/*.sql`, via the `demo-seed` compose
service) — there is nothing to seed by hand.
"""
import asyncio
import json
import urllib.error
import urllib.request
import uuid

import websockets

API, WS = "http://api:8000", "ws://api:8000/ws/chat"
SID = "flow-" + uuid.uuid4().hex[:8]
JAX = "https://github.com/dbt-labs/jaffle_shop"
CONN = "postgresql://qualitest:qualitest@db:5432/qualitest"
TABLES = ["raw_customers", "raw_orders", "raw_payments", "customers", "orders"]


async def rest(path, body=None):
    """Blocking urllib call, off the event loop.

    A slow endpoint (the agent calls the LLM on /from-repo and /from-url) must
    not freeze the WebSocket reader: uvicorn sends keepalive pings, and a
    blocked loop never answers them, so the server drops the connection.
    """
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API + path, data=data, method="POST" if data else "GET",
        headers={"content-type": "application/json"} if data else {},
    )

    def call():
        try:
            with urllib.request.urlopen(req, timeout=240) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return {"__http__": e.code, "detail": e.read().decode()[:200]}

    return await asyncio.to_thread(call)


async def drain(ws, until=("done", "error"), timeout=300):
    out = []
    while True:
        ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        t, d = ev["type"], ev.get("data", {})
        if t != "check.completed":
            out.append((t, d))
        if t in until:
            return out


async def main():
    async with websockets.connect(WS, max_size=32 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"session_id": SID}))
        boot = []
        while True:
            ev = json.loads(await ws.recv())
            boot.append((ev["type"], ev.get("data", {})))
            if ev["type"] == "history":
                break
        # the pending question is sent straight after history
        try:
            ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            boot.append((ev["type"], ev.get("data", {})))
        except (asyncio.TimeoutError, StopAsyncIteration):
            pass
        types = [t for t, _ in boot]
        assert "agent.question" in types, types
        print("[boot]", types)

        # ---------- opening question asks for the codebase, repo first ----------
        ctx = await rest(f"/api/sessions/{SID}/context")
        question = ctx["pending_question"]
        assert question, "a pending question should be recorded on the session"
        kinds = [o["kind"] for o in question["options"]]
        print("[Q1]", question["title"], "|", kinds)
        assert kinds == ["git_url", "file_upload", "paste"], kinds
        assert [o["preferred"] for o in question["options"]] == [True, True, False]

        # ---------- supply a repo via REST, notify the agent ----------
        res = await rest("/api/etl/from-repo", {"url": JAX, "session_id": SID})
        assert "analysis" in res, res
        eid = res["analysis"]["id"]
        reqs = res["required_sources"]
        print("[repo]", len(res["analysis"]["files"]), "files ->", len(reqs), "requirement(s)",
              reqs[0]["tables"][:5])

        await ws.send(json.dumps({"content": "", "action": "etl_provided",
                                  "payload": {"etl_id": eid}}))
        out = await drain(ws)
        types = [t for t, _ in out]
        print("[after repo]", types)
        assert "agent.question" in types, types
        q2 = next(d for t, d in out if t == "agent.question")
        assert [o["kind"] for o in q2["options"]] == ["connection"], q2["options"]
        key = q2["requirement_key"]
        prefill = q2["options"][0]["prefill"]
        print("[Q2]", q2["title"], "| key:", key, "| prefill:", prefill)

        # ---------- answering with a db that lacks the tables must not pass ----------
        bad = await rest("/api/connections/from-url",
                         {"url": CONN, "session_id": SID, "requirement_key": key,
                          "tables": ["table_that_does_not_exist"]})
        print("[wrong tables] ok:", bad["status"]["ok"], "|", (bad["status"]["message"] or "")[:80])
        assert bad["status"]["ok"] is False

        # ---------- correct answer ----------
        good = await rest("/api/connections/from-url",
                          {"url": CONN, "session_id": SID, "requirement_key": key,
                           "tables": reqs[0]["tables"]})
        print("[connect] ok:", good["status"]["ok"], "| matched:", good["matched_tables"],
              "| unmatched:", good["unmatched_tables"])
        assert good["status"]["ok"] and good["connection_id"], good["status"]
        cid = good["connection_id"]

        await ws.send(json.dumps({"content": "", "action": "connection_provided",
                                  "payload": {"connection_id": cid, "requirement_key": key}}))
        out = await drain(ws)
        types = [t for t, _ in out]
        print("[after connect]", types)
        assert "plan.ready" in types, types
        checks = next(d for t, d in out if t == "plan.ready")["checks"]
        print("[plan]", len(checks), "checks")
        for c in checks[:8]:
            print("   [{:8}] {:12} {:22} {}".format(
                c["severity"], c["kind"], c["table"], c["column"] or ""))

        # ---------- approve ----------
        await ws.send(json.dumps({"content": "run the tests", "action": "approve"}))
        out = await drain(ws)
        done = [d for t, d in out if t == "run.completed"]
        assert done, out
        score = done[-1]["score"]
        print("\n[run] score:", score["overall"], "| passed:", score["passed"],
              "| failed:", score["failed"], "| errored:", score["errored"])
        assert score["errored"] == 0, "no check should error"
        for r in done[-1]["results"]:
            if r["status"] in ("failed", "error"):
                print("   {:6} {:36} {}".format(r["status"], r["name"][:36], (r["message"] or "")[:64]))

        # ---------- secrets must not be in the transcript ----------
        hist = await rest(f"/api/sessions/{SID}/messages")
        blob = json.dumps(hist)
        leaked = [frag for frag in ("qualitest:qualitest@", "qualitest:qualitest@db") if frag in blob]
        assert not leaked, f"CREDENTIAL LEAK: {leaked}"
        assert "password" not in blob.lower() or "***" in blob
        print("\n[transcript]", len(hist["messages"]), "messages, masked:",
              "***" in blob or "no url in transcript")

        print("\nFLOW OK")


asyncio.run(main())
