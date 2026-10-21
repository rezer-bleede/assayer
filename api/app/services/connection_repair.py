"""Automatic connection repair.

A failed connection is usually not a typo but an *environment* mismatch, and
the two that dominate are already known to us:

  1. The agent runs inside a container. ``localhost`` is the container itself,
     so a database on the user's machine is never under that name; the host is
     reachable as ``host.docker.internal`` (Docker Desktop).
  2. ClickHouse speaks HTTP on 8123/8443; ports 9000-9009/9440 are the native
     TCP protocol, which the HTTP driver cannot use at all.

The repair loop therefore tries a **deterministic ladder** of such fixes first
(free, instant, no hallucination), then — only if the ladder is exhausted and
an LLM is configured — asks the model for the next most plausible candidates,
feeding each round the failures so far. Every candidate is validated (never a
type change, never a credential change) and de-duplicated, and every attempt is
streamed to the caller so the user watches the search rather than a spinner.

The loop is bounded by ``connection_repair_*`` settings: "as many tries as
needed" is honoured within those limits, and hitting a limit is disclosed in
the returned report rather than hidden.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.connectors.factory import build_connector
from app.core.config import settings
from app.core.logging import log
from app.schemas import (
    ConnectionSpec,
    ConnectionStatus,
    RepairAttempt,
    RepairReport,
    SourceType,
)
from app.services.requirements import split_ref

EmitFn = Callable[[str, dict[str, Any]], None]


@dataclass
class AttemptOutcome:
    """What one connection attempt did — the only thing the loop needs.

    ``matched``/``unmatched``/``status`` are carried through untouched so the
    caller gets the winning probe's full result without connecting twice.
    """

    ok: bool
    message: str = ""
    matched: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    status: Any = None


AttemptFn = Callable[[ConnectionSpec], AttemptOutcome]
ListDatabasesFn = Callable[[ConnectionSpec], list[str]]

# `localhost` from inside the agent container is the container: a database on
# the user's own machine is never reachable under that name from here.
_LOOPBACK = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"}
_HOST_ALIASES = ("host.docker.internal",)

# ClickHouse's native TCP protocol — the HTTP driver cannot speak it at all.
_NATIVE_CH_PORTS = {9000, 9002, 9009, 9440}
_CH_HTTP_PORTS = (8123, 8443)

_DEFAULT_PORTS: dict[SourceType, int] = {
    SourceType.POSTGRES: 5432,
    SourceType.MYSQL: 3306,
    SourceType.MSSQL: 1433,
    SourceType.SNOWFLAKE: 443,
    SourceType.CLICKHOUSE: 8123,
}

_DB_MISSING_RE = re.compile(
    r"does not exist|unknown database|no such database|unknown (?:database|role|catalog)",
    re.I,
)
# never worth trying as the user's database
_SYSTEM_DBS = {
    "system",
    "default",
    "information_schema",
    "mysql",
    "performance_schema",
    "sys",
    "pg_catalog",
    "postgres",
    "template0",
    "template1",
    "admin",
}
_HOST_RE = re.compile(r"^[A-Za-z0-9._:\-\[\]]+$")


@dataclass
class RepairResult:
    attempts: list[RepairAttempt] = field(default_factory=list)
    suggested: ConnectionSpec | None = None
    outcome: AttemptOutcome | None = None
    note: str | None = None
    exhausted: bool = False

    @property
    def ok(self) -> bool:
        """True if *some* configuration connected (original or repaired)."""
        return bool(self.outcome and self.outcome.ok)

    @property
    def repaired(self) -> bool:
        """True when the working configuration differs from the one given."""
        return self.suggested is not None

    def report(self) -> RepairReport:
        return RepairReport(
            attempted=len(self.attempts),
            exhausted=self.exhausted,
            suggested=self.suggested.redacted() if self.suggested else None,
            attempts=self.attempts,
            note=self.note,
        )


# --------------------------------------------------------------------------- #
# candidate generation
# --------------------------------------------------------------------------- #


def _host_options(spec: ConnectionSpec, hints: dict) -> list[tuple[str, str]]:
    opts: list[tuple[str, str]] = []
    host = (spec.host or "").strip().lower()
    if host in _LOOPBACK:
        opts += [
            (h, f"'{spec.host}' is the agent's own container — your machine is at {h}")
            for h in _HOST_ALIASES
        ]
    declared = hints.get("host")
    if declared and declared != spec.host:
        opts.append((declared, "host declared in your own code"))
    return opts


def _port_options(spec: ConnectionSpec, hints: dict) -> list[tuple[int, str]]:
    opts: list[tuple[int, str]] = []
    if spec.type == SourceType.CLICKHOUSE and spec.port in _NATIVE_CH_PORTS:
        opts += [
            (
                p,
                f"port {spec.port} is ClickHouse's native TCP protocol; "
                f"the HTTP interface is on {p}",
            )
            for p in _CH_HTTP_PORTS
        ]
    declared = hints.get("port")
    if isinstance(declared, int) and declared != spec.port:
        opts.append((declared, "port declared in your own code"))
    std = _DEFAULT_PORTS.get(spec.type)
    if std and spec.port and spec.port != std and spec.port not in _NATIVE_CH_PORTS:
        opts.append((std, f"{spec.type} usually listens on {std}"))
    return opts


def _db_options(spec: ConnectionSpec, hints: dict, databases: list[str]) -> list[tuple[str, str]]:
    """Databases worth trying, best guess first.

    A `does not exist` error proves the server was reachable, so the database
    *name* is the suspect. We rank names the model may not even know by
    similarity, then by "the server has it" — and, crucially, the probe itself
    decides: a candidate is only kept if the connection then holds the tables
    the ETL needs, so proposing a few names is safe.
    """
    opts: list[tuple[str, str]] = []
    declared = hints.get("database")
    if declared and declared != spec.database:
        opts.append((declared, "database declared in your own code"))
    if databases and spec.database:
        candidates = [d for d in databases if d.lower() not in _SYSTEM_DBS and d != spec.database]
        close = difflib.get_close_matches(spec.database, candidates, n=2, cutoff=0.4)
        for match in (close or candidates)[:3]:
            if close:
                why = f"'{match}' is the closest database that exists to '{spec.database}'"
            else:
                why = f"'{spec.database}' does not exist; testing '{match}' against your tables"
            opts.append((match, why))
    return opts


def _required_fixes(spec: ConnectionSpec) -> set[str]:
    """Fields proven wrong by the failure itself.

    A refused ``localhost`` and a ClickHouse native-protocol port are both
    *known* to be wrong, so any candidate that keeps either one is doomed — and
    trying it is how a small attempt budget gets wasted on combinations that
    could never have worked. Candidates must change every required field.
    """
    required: set[str] = set()
    if (spec.host or "").strip().lower() in _LOOPBACK:
        required.add("host")
    if spec.type == SourceType.CLICKHOUSE and spec.port in _NATIVE_CH_PORTS:
        required.add("port")
    return required


def _grid(
    spec: ConnectionSpec,
    hosts: list[tuple[str, str]],
    ports: list[tuple[int, str]],
    dbs: list[tuple[str, str]],
    limit: int,
    required: set[str] | None = None,
) -> list[tuple[dict[str, Any], str]]:
    """Cross-product of single-field fixes, fewest changes first.

    The failure that motivated this feature needed *both* a host and a port
    fix at once, so single-change candidates alone are not enough — the
    cross-product is ordered by how many fields each candidate touches, and
    candidates missing a required fix are dropped outright.
    """
    must = required or set()
    out: list[tuple[int, dict[str, Any], str]] = []
    for hv, hr in [(None, "")] + hosts:
        for pv, pr in [(None, "")] + ports:
            for dv, dr in [(None, "")] + dbs:
                if hv is None and pv is None and dv is None:
                    continue
                updates: dict[str, Any] = {}
                if hv is not None:
                    updates["host"] = hv
                if pv is not None:
                    updates["port"] = pv
                if dv is not None:
                    updates["database"] = dv
                if not must <= set(updates):
                    continue
                reason = "; ".join(r for r in (hr, pr, dr) if r)
                out.append((len(updates), updates, reason))
    out.sort(key=lambda item: item[0])
    return [(updates, reason) for _, updates, reason in out[:limit]]


# --------------------------------------------------------------------------- #
# keying / describing
# --------------------------------------------------------------------------- #


def _key(spec: ConnectionSpec) -> tuple:
    return (
        (spec.host or "").strip().lower(),
        spec.port,
        (spec.database or "").strip().lower(),
    )


def _describe(spec: ConnectionSpec) -> str:
    db = f"/{spec.database}" if spec.database else ""
    return f"{spec.host}:{spec.port}{db}"


def _emit(emit: EmitFn | None, event: str, data: dict[str, Any]) -> None:
    if emit is None:
        return
    try:
        emit(event, data)
    except Exception:  # noqa: BLE001 - progress must never break the repair
        log.debug("repair emit failed", exc_info=True)


# --------------------------------------------------------------------------- #
# LLM round
# --------------------------------------------------------------------------- #


def _parse_json_block(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip(), flags=re.I)
    try:
        data = json.loads(cleaned)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            try:
                data = json.loads(cleaned[start : end + 1])
                return data if isinstance(data, dict) else {}
            except json.JSONDecodeError:
                return {}
    return {}


def _apply_llm_candidate(spec: ConnectionSpec, raw: dict) -> ConnectionSpec | None:
    """A validated host/port/database patch — never type, user or password."""
    if not isinstance(raw, dict):
        return None
    updates: dict[str, Any] = {}
    host = raw.get("host")
    if isinstance(host, str) and host.strip() and _HOST_RE.match(host.strip()):
        updates["host"] = host.strip()
    port = raw.get("port")
    if port is not None:
        try:
            parsed = int(port)
        except (TypeError, ValueError):
            parsed = 0
        if 1 <= parsed <= 65535:
            updates["port"] = parsed
    database = raw.get("database")
    if isinstance(database, str) and database.strip():
        updates["database"] = database.strip()[:128]
    if not updates:
        return None
    return spec.model_copy(update=updates)


async def _llm_candidates(
    spec: ConnectionSpec,
    failures: list[dict[str, Any]],
    hints: dict,
    databases: list[str],
) -> list[dict]:
    from app.agent.prompts import repair_prompt
    from app.core.llm import ainvoke, llm_available

    if not llm_available():
        return []
    prompt = repair_prompt(spec.redacted(), failures, hints, databases)
    raw = await ainvoke(prompt, max_tokens=400)
    data = _parse_json_block(raw)
    candidates = data.get("candidates")
    if not isinstance(candidates, list):
        return []
    return [c for c in candidates if isinstance(c, dict)][:3]


# --------------------------------------------------------------------------- #
# the loop
# --------------------------------------------------------------------------- #


async def repair_connection(
    spec: ConnectionSpec,
    *,
    attempt: AttemptFn,
    hints: dict[str, Any] | None = None,
    list_databases: ListDatabasesFn | None = None,
    emit: EmitFn | None = None,
) -> RepairResult:
    """Retry a failing connection with better-guessed addressing.

    ``attempt`` performs one connection try and reports whether it worked;
    ``emit`` receives ``connection.attempt`` progress dicts as they happen. The
    configuration as given is always attempted first (attempt #1), so a caller
    can use this as *the* connection path: ``result.ok`` says whether anything
    connected, and ``result.repaired`` whether that differed from the input.
    """
    hints = hints or {}
    result = RepairResult()
    started = time.monotonic()
    tried: set[tuple] = {_key(spec)}
    failures: list[dict[str, Any]] = []
    databases: list[str] = []
    enumerated = False
    # the candidate that last reached a server; a `database does not exist`
    # error proves *that* address works, so it is the one to enumerate from
    reachable: ConnectionSpec = spec

    def budget_left() -> bool:
        return (
            len(result.attempts) < settings.connection_repair_max_attempts
            and (time.monotonic() - started) < settings.connection_repair_timeout_seconds
        )

    async def do_attempt(
        cand: ConnectionSpec, source: str, reason: str, *, mark_suggested: bool = True
    ) -> bool:
        nonlocal reachable
        n = len(result.attempts) + 1
        outcome = await asyncio.to_thread(attempt, cand)
        result.attempts.append(
            RepairAttempt(
                n=n,
                source=source,
                host=cand.host,
                port=cand.port,
                database=cand.database,
                outcome="ok" if outcome.ok else "failed",
                note=(reason if outcome.ok else f"{reason} — {outcome.message}")[:300],
            )
        )
        _emit(
            emit,
            "connection.attempt",
            {
                "n": n,
                "source": source,
                "host": cand.host,
                "port": cand.port,
                "database": cand.database,
                "outcome": "ok" if outcome.ok else "failed",
                "note": reason,
                "error": None if outcome.ok else outcome.message[:200],
            },
        )
        if outcome.ok:
            result.outcome = outcome
            if mark_suggested:
                result.suggested = cand
                result.note = (
                    f"connected using {_describe(cand)} ({reason})"
                    if reason
                    else f"connected using {_describe(cand)}"
                )
            return True
        if source == "original":
            # remember how the given configuration failed, so the caller can
            # report that original error when nothing better is found
            result.outcome = outcome
        # a `database does not exist` error means the driver got an answer, so
        # this candidate's addressing is good — remember it for enumeration
        if _DB_MISSING_RE.search(outcome.message):
            reachable = cand
        failures.append(
            {
                "host": cand.host,
                "port": cand.port,
                "database": cand.database,
                "error": outcome.message[:200],
            }
        )
        return False

    def make_grid() -> deque[tuple[dict[str, Any], str]]:
        return deque(
            _grid(
                spec,
                _host_options(spec, hints),
                _port_options(spec, hints),
                _db_options(spec, hints, databases),
                limit=settings.connection_repair_max_attempts * 3,
                required=_required_fixes(spec),
            )
        )

    async def maybe_enumerate() -> bool:
        """On a `database does not exist` failure, ask the server what exists.

        A wrong database *name* is invisible to the host/port ladder — the
        connection itself succeeds and the error names the missing database —
        so this is the only way to find the real one, and it is worth the trip.
        """
        nonlocal enumerated, databases
        if enumerated or list_databases is None or not failures:
            return False
        if not _DB_MISSING_RE.search(failures[-1]["error"]):
            return False
        enumerated = True
        databases = await asyncio.to_thread(_safe_list_databases, list_databases, reachable)
        return bool(databases)

    # ---- 0. the connection exactly as given ------------------------------ #
    if await do_attempt(spec, "original", "the connection as given", mark_suggested=False):
        result.note = "connected on the first try; no repair needed"
        return result
    if not settings.connection_repair_enabled:
        result.exhausted = True
        result.note = "automatic repair is disabled"
        return result

    # ---- 1. deterministic ladder ----------------------------------------- #
    await maybe_enumerate()
    pending = make_grid()
    while pending and budget_left():
        updates, reason = pending.popleft()
        cand = spec.model_copy(update=updates)
        if _key(cand) in tried:
            continue
        tried.add(_key(cand))
        if await do_attempt(cand, "deterministic", reason):
            return result
        if not settings.connection_repair_enabled:
            break
        if await maybe_enumerate():
            pending = make_grid()

    # ---- 2. LLM rounds (only if the ladder was exhausted) ---------------- #
    if settings.connection_repair_enabled and budget_left() and settings.llm_configured:
        for _round in range(settings.connection_repair_max_llm_rounds):
            if not budget_left():
                break
            try:
                candidates = await _llm_candidates(spec, failures, hints, databases)
            except Exception:  # noqa: BLE001 - the LLM is an enhancement
                log.warning("repair LLM round failed", exc_info=True)
                break
            progressed = False
            for raw in candidates:
                if not budget_left():
                    break
                llm_spec = _apply_llm_candidate(spec, raw)
                if llm_spec is None or _key(llm_spec) in tried:
                    continue
                tried.add(_key(llm_spec))
                progressed = True
                reason = str(raw.get("reason") or "suggested by the model")[:200]
                if await do_attempt(llm_spec, "llm", reason):
                    return result
            if not progressed:
                break

    result.exhausted = True
    if result.attempts:
        result.note = (
            f"tried {len(result.attempts)} configuration(s); none could connect "
            "within the repair limits"
        )
    else:
        result.note = "no alternative configuration to try"
    return result


def _safe_list_databases(fn: ListDatabasesFn, spec: ConnectionSpec) -> list[str]:
    """Enumerate databases on the server with the database name omitted.

    Called only after a `database does not exist` failure: the client could be
    created without naming a database, so this usually still works.
    """
    try:
        return fn(spec.model_copy(update={"database": None})) or []
    except Exception:  # noqa: BLE001 - enumeration is best-effort
        log.debug("listing databases for repair failed", exc_info=True)
        return []


# --------------------------------------------------------------------------- #
# production implementations of the callables
# --------------------------------------------------------------------------- #


def probe(
    spec: ConnectionSpec, tables: list[str] | None = None
) -> tuple[ConnectionStatus, list[str], list[str]]:
    """Connect once and report which declared tables are present. Never persists.

    Answering a datasource question with the wrong database is common, so we
    report which declared tables were found rather than failing silently later
    at plan time — which also makes a candidate database *self-verifying*: a
    repaired connection only counts if it actually holds the ETL's tables.
    """
    matched: list[str] = []
    unmatched: list[str] = []
    declared = list(tables or [])
    conn = build_connector(spec)
    try:
        status = conn.test()
        if status.ok and declared:
            present = {
                _bare(t.get("name", ""))
                for t in conn.introspect(max_tables=200).get("tables", [])
                if isinstance(t, dict) and t.get("name")
            }
            for ref in declared:
                (matched if _bare(ref) in present else unmatched).append(ref)
    finally:
        conn.dispose()

    if unmatched and not matched:
        status.ok = False
        status.message = (
            f"Connected, but none of the expected tables are here "
            f"({', '.join(unmatched[:4])}). Check the database and schema."
        )
    elif unmatched:
        status.message = (
            f"{status.message or ''} Found {len(matched)}/{len(declared)} expected "
            f"table(s); missing: {', '.join(unmatched[:4])}."
        ).strip()
    return status, matched, unmatched


def probe_attempt(spec: ConnectionSpec, tables: list[str] | None = None) -> AttemptOutcome:
    """`probe` shaped for the repair loop, keeping the winning status."""
    status, matched, unmatched = probe(spec, tables)
    return AttemptOutcome(
        ok=status.ok,
        message=status.message or "",
        matched=matched,
        unmatched=unmatched,
        status=status,
    )


def _bare(ref: str) -> str:
    """`raw.orders` -> `orders`, so declared refs match introspected names."""
    return split_ref(str(ref))[1] or str(ref).lower()


def list_server_databases(spec: ConnectionSpec) -> list[str]:
    """Enumerate the databases (schemas) that actually exist on a server."""
    conn = build_connector(spec)
    try:
        return conn.list_schemas()
    finally:
        conn.dispose()
