"""Long-running worker for heavy test executions.

Pulls approved plans off the Redis queue, executes them, streams progress back
into Redis pub/sub (the API relays that to connected WebSocket clients), and
persists results. Keeps long/expensive data checks off the API event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
from typing import Any

from app.core.config import settings
from app.core.logging import configure_logging, log
from app.schemas import ConnectionSpec, RunResult
from app.services.orchestrator import run_audit
from app.store import repository as repo
from app.store.db import init_db

QUEUE = "qualitest:jobs"
PROGRESS_CHANNEL = "qualitest:progress"
_results: list[RunResult] = []


def _redis() -> Any:
    import redis

    return redis.from_url(settings.redis_url, decode_responses=True)


async def publish_progress(event: str, data: dict) -> None:
    try:
        await asyncio.to_thread(
            _redis().publish,
            PROGRESS_CHANNEL,
            json.dumps({"type": event, "run_id": data.get("run_id"), "data": data}),
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("progress publish failed", error=str(exc))


async def process_job(job: dict) -> None:
    run_id = job.get("run_id")
    session_id = job.get("session_id")
    plan = repo.get_plan(run_id) if run_id else None
    if plan is None:
        log.warning("job has no plan", run_id=run_id)
        return

    connections: dict[str, ConnectionSpec] = {}
    for cid in job.get("connection_ids", []):
        spec = repo.get_connection(cid)
        if spec:
            connections[cid] = spec
    if not connections:
        log.warning("job has no connections", run_id=run_id)
        return

    log.info("executing plan", run_id=run_id, checks=len(plan.checks))
    analysis = repo.get_etl(job.get("etl_id")) if job.get("etl_id") else None

    def emit(event: str, data: dict) -> None:
        asyncio.create_task(
            publish_progress(event, {**data, "run_id": run_id, "session_id": session_id})
        )

    result = await run_audit(run_id, plan, connections, analysis, session_id, emit=emit)
    _results.append(result)
    await publish_progress(
        "run.completed",
        {"run_id": run_id, "session_id": session_id, "score": result.score.overall},
    )


async def worker_loop() -> None:
    r = _redis()
    await asyncio.to_thread(r.ping)
    log.info("worker ready", queue=QUEUE)
    while True:
        try:
            item = await asyncio.to_thread(r.brpop, QUEUE, timeout=5)
            if item is None:
                continue
            _, payload = item
            job = json.loads(payload)
            await process_job(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("worker loop error")
            await asyncio.sleep(2)
            del exc


def enqueue(run_id: str, session_id: str, connection_ids: list[str], etl_id: str | None) -> None:
    _redis().rpush(
        QUEUE,
        json.dumps(
            {
                "run_id": run_id,
                "session_id": session_id,
                "connection_ids": connection_ids,
                "etl_id": etl_id,
            }
        ),
    )


async def main() -> None:
    configure_logging()
    init_db()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    tasks = [asyncio.create_task(worker_loop())]
    await stop.wait()
    log.info("worker shutting down")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
