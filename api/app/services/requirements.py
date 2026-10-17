"""Derive the connections an ETL actually needs.

Given a parsed ETL, answer: *how many separate databases must I connect to, and
what do I already know about each?* The chat then asks exactly one question per
returned requirement, pre-filled from the strongest available evidence.

Evidence order, strongest first:
  1. dbt `profiles.yml` / manifest — declares real database names per source.
  2. Connection strings embedded in the code itself (`create_engine(...)`),
     which frequently hand us host + database + user for free.
  3. Schema prefixes in table references (`raw.orders`) — a weak proxy for
     "these tables probably live together".
"""

from __future__ import annotations

import re

from app.schemas import ConnectionSpec, EtlAnalysis, SourceRequirement, SourceType
from app.services.ingest import coerce_port, extract_connections

# `schema.table` / `db.schema.table` / quoted or backticked variants
_SPLIT_RE = re.compile(r"^(?:(?P<a>[\w\"`\[\]]+)\.)?(?P<b>[\w\"`\[\]]+)$")

_DEFAULT_PORTS: dict[str, int] = {
    str(SourceType.POSTGRES): 5432,
    str(SourceType.MYSQL): 3306,
    str(SourceType.MSSQL): 1433,
    str(SourceType.SNOWFLAKE): 443,
    str(SourceType.CLICKHOUSE): 8123,
}


def _clean(part: str | None) -> str | None:
    if not part:
        return None
    return part.strip('`"[]').lower() or None


def _engine_type(raw: object) -> SourceType | None:
    """Best-effort engine hint.

    dbt projects name adapters we cannot connect to — `clickhouse`, `redshift`,
    `trino`, `presto`, `oracle` … The *host + database* evidence is still real
    and must survive the parse; only the engine prefill is dropped. A crash
    here would take down the whole upload for a hint that is, at worst, wrong.
    """
    if not raw:
        return None
    try:
        return SourceType(str(raw))
    except ValueError:
        return None


def split_ref(ref: str) -> tuple[str | None, str]:
    """`raw.orders` -> (`raw`, `orders`); `orders` -> (None, `orders`)."""
    m = _SPLIT_RE.match(ref.strip())
    if not m:
        return None, _clean(ref) or ref
    a = _clean(m.group("a"))
    b = _clean(m.group("b")) or ref
    return a, b


def extract_connection_hints(code_blob: str) -> list[dict]:
    """Redacted connection specs found inside the ETL source.

    Never returns credentials — only what is safe to echo back to the client.
    """
    hints: list[dict] = []
    for spec in extract_connections(code_blob or ""):
        hints.append(
            {
                "type": str(spec.type),
                "host": spec.host,
                "port": spec.port or _DEFAULT_PORTS.get(str(spec.type)),
                "database": spec.database,
                "schema": spec.schema_,
                "username": spec.username,
                "account": spec.account,
                "warehouse": spec.warehouse,
            }
        )
    return hints


def _match_hint(hint: dict, tables: list[str], database: str | None) -> bool:
    """Does this connection plausibly serve these tables?"""
    prefixes = {p for p, _ in (split_ref(t) for t in tables) if p}
    if hint.get("database") and database and hint["database"].lower() == database.lower():
        return True
    if hint.get("schema") and hint["schema"].lower() in prefixes:
        return True
    return any(prefix and prefix == (hint.get("database") or "").lower() for prefix in prefixes)


def derive_requirements(analysis: EtlAnalysis) -> list[SourceRequirement]:
    """Group every table the ETL touches into one requirement per database."""
    targets = {_clean(t) or t for t in analysis.targets}
    refs: list[str] = []
    for ref in [*analysis.sources, *analysis.targets]:
        if ref not in refs:
            refs.append(ref)

    if not refs:
        return []

    hints = analysis.connection_hints or []
    single_hint = hints[0] if len(hints) == 1 else None

    groups: dict[str, dict] = {}
    for ref in refs:
        prefix, bare = split_ref(ref)
        hint_meta = (analysis.table_hints or {}).get(ref) or {}
        # a bare table name that is also a known target belongs to the target db
        role = "target" if bare in targets or ref in targets else "source"

        database = _clean(hint_meta.get("database")) or prefix
        schema = _clean(hint_meta.get("schema")) or prefix
        key = f"db:{database}" if database else "default"

        g = groups.setdefault(
            key,
            {
                "tables": [],
                "database": database,
                "schema": schema,
                "roles": set(),
                "origin": hint_meta.get("origin")
                or ("dbt profiles" if hint_meta.get("database") else "sql schema prefix"),
                "confidence": 0.9 if hint_meta.get("database") else (0.6 if database else 0.4),
            },
        )
        if ref not in g["tables"]:
            g["tables"].append(ref)
        g["roles"].add(role)
        # trust the strongest evidence seen for the group
        if hint_meta.get("database"):
            g["origin"] = "dbt profiles"
            g["confidence"] = 0.9

    requirements: list[SourceRequirement] = []
    for key, g in groups.items():
        roles = g["roles"]
        role: str = "mixed" if len(roles) > 1 else next(iter(roles))

        engine: SourceType | None = None
        host = port = database = user = None
        matched = next((h for h in hints if _match_hint(h, g["tables"], g["database"])), None)
        if matched is None and single_hint is not None:
            # one connection in the whole codebase -> it serves everything
            matched = single_hint
            if g["confidence"] < 0.9:
                g["confidence"] = 0.75
        if matched:
            engine = _engine_type(matched.get("type"))
            host, database = matched.get("host"), matched.get("database") or g["database"]
            # hints reach us from YAML *and* chat JSON — coerce at the boundary
            port, user = coerce_port(matched.get("port")), matched.get("username")
            g["origin"] = "engine url in code"
            g["confidence"] = max(g["confidence"], 0.85)

        requirements.append(
            SourceRequirement(
                key=key,
                tables=sorted(g["tables"]),
                database=database or g["database"],
                schema_=g["schema"],
                engine_hint=engine,
                host_hint=host,
                port_hint=port,
                user_hint=user,
                role=role,  # type: ignore[arg-type]
                origin=g["origin"],
                confidence=round(g["confidence"], 2),
            )
        )

    # sources first, then anything holding targets; busiest group first
    requirements.sort(key=lambda r: (r.is_target_side, -len(r.tables), r.key))
    return requirements


def annotate(analysis: EtlAnalysis, code_blob: str = "") -> EtlAnalysis:
    """Attach connection hints + requirements. Mutates and returns `analysis`."""
    haystack = code_blob or analysis.raw_code
    if not haystack and analysis.files:
        haystack = "\n".join(analysis.files)
    if not analysis.connection_hints:
        analysis.connection_hints = extract_connection_hints(haystack)
    analysis.required_sources = derive_requirements(analysis)
    return analysis


def to_spec(requirement: SourceRequirement, url_or_fields: ConnectionSpec) -> ConnectionSpec:
    """Merge an answered connection into the requirement's declared table list.

    `options.tables` makes later binding deterministic instead of name-fuzzy.
    """
    spec = url_or_fields.model_copy(update={})
    options = dict(spec.options or {})
    options.setdefault("tables", requirement.tables)
    if requirement.role in ("target", "mixed") and requirement.tables:
        options.setdefault("role", requirement.role)
    if requirement.schema_ and not spec.schema_:
        spec.schema_ = requirement.schema_
    spec.options = options
    return spec
