"""dbt analysis from `target/manifest.json` (preferred) or from model .sql files.

Manifest gives us authoritative parent/child lineage, columns and tests.
"""

from __future__ import annotations

import json
import os

import yaml

from app.schemas import EtlLanguage, LineageEdge, Transformation
from app.services.ingest import coerce_port, jinja_value

_TEST_HINTS = {
    "not_null": "column must never be NULL",
    "unique": "column values must be unique",
    "accepted_values": "column must be within an allowed set",
    "relationships": "FK must exist in the referenced table",
    "expression_is_true": "custom boolean expression must hold",
}


def parse_dbt_manifest(manifest: dict, label: str | None = None):
    from app.schemas import EtlAnalysis

    transformations: list[Transformation] = []
    edges: list[LineageEdge] = []
    sources: list[str] = []
    targets: list[str] = []
    risks: list[str] = []

    nodes = manifest.get("nodes", {})
    sources_map = manifest.get("sources", {})
    # dbt lists seeds under `seeds`; in a manifest they are nodes of that type
    seeds_map = {
        **{k: v for k, v in nodes.items() if v.get("resource_type") == "seed"},
        **manifest.get("seeds", {}),
    }

    def resolve(name: str) -> str:
        """Map a manifest node id to a friendly `db.table` reference."""
        if name in sources_map:
            src = sources_map[name]
            db, sname = src.get("source_name", ""), src.get("name")
            explicit = src.get("database")
            if explicit:
                return f"{explicit}.{sname}" if sname else explicit
            return f"{db}.{sname}" if sname else db
        return nodes.get(name, {}).get("name") or name.split(".")[-1]

    def add_edges(child: str, parents: list[str], kind: str) -> None:
        for p in parents:
            source = resolve(p)
            edges.append(LineageEdge(source=source, target=child))
            if p in seeds_map:
                # a seed is a CSV promoted to a real table: an input, not a model
                sources.append(source)
                transformations.append(
                    Transformation(
                        kind="seed",
                        description=f"'{source}' is a dbt seed loaded from a CSV",
                        source=source,
                        target=child,
                        confidence=1.0,
                    )
                )
            elif p in nodes:
                transformations.append(
                    Transformation(
                        kind="ref",
                        description=f"{kind} '{child}' depends on '{source}'",
                        source=source,
                        target=child,
                        confidence=1.0,
                    )
                )
            elif p in sources_map:
                sources.append(source)
                transformations.append(
                    Transformation(
                        kind="source",
                        description=f"model '{child}' reads from source '{source}'",
                        source=source,
                        target=child,
                        confidence=1.0,
                    )
                )

    primary_models: dict[str, str] = {}
    for node_id, node in nodes.items():
        if node.get("resource_type") not in {"model", "snapshot"}:
            continue
        child = node.get("name") or node_id.split(".")[-1]
        targets.append(child)

        parents = node.get("depends_on", {}).get("nodes", [])
        for parent in parents:
            add_edges(child, [parent], "model")
        # the first source/seed a model reads sets its grain
        for parent in parents:
            if parent in sources_map or parent in seeds_map:
                primary_models[child] = resolve(parent)
                break

        cols = node.get("columns", {})
        col_names = list(cols.keys())
        mat = node.get("config", {}).get("materialized", "table")
        transformations.append(
            Transformation(
                kind="materialization",
                description=f"dbt model '{child}' materialized as {mat}",
                target=child,
                fields=col_names,
                confidence=1.0,
            )
        )

        for col, meta in cols.items():
            tests = [t for t in meta.get("data_tests", meta.get("tests", []))]
            if meta.get("description"):
                transformations.append(
                    Transformation(
                        kind="column",
                        description=f"{child}.{col}: {meta['description']}",
                        target=child,
                        fields=[col],
                        confidence=0.9,
                    )
                )
            for t in tests:
                tname = t if isinstance(t, str) else list(t.keys())[0]
                transformations.append(
                    Transformation(
                        kind="dbt_test",
                        description=f"dbt test {tname} on {child}.{col}",
                        target=child,
                        fields=[col],
                        expression=_TEST_HINTS.get(tname),
                        confidence=1.0,
                    )
                )

        if any(t in _TEST_HINTS for t in
               [x for c in cols.values() for x in c.get("data_tests", c.get("tests", []))]):
            pass
        elif cols:
            risks.append(
                f"dbt model '{child}' has no column-level tests declared; "
                "agent will synthesize null/unique/range checks from metadata"
            )

    risks.extend(_global_risks(manifest))
    if not risks:
        risks.append("dbt project analysed: no structural risk flagged")

    return EtlAnalysis(
        language=EtlLanguage.DBT,
        label=label,
        transformations=transformations,
        lineage=edges,
        sources=_dedupe(sources),
        targets=_dedupe(targets),
        primary_sources=_dedupe(list(primary_models.values())),
        grain=dict(primary_models),
        risks=_dedupe(risks),
        raw_code="",
    )


def _global_risks(manifest: dict) -> list[str]:
    out: list[str] = []
    nodes = manifest.get("nodes", {})
    incremental = [n.get("name") for n in nodes.values() if n.get("config", {}).get("materialized") == "incremental"]
    if incremental:
        out.append(
            f"Incremental models {incremental}: not idempotent by default; "
            "verify uniqueness and row-count growth per run"
        )
    eph = [n.get("name") for n in nodes.values() if n.get("config", {}).get("materialized") == "ephemeral"]
    if eph:
        out.append(f"Ephemeral models {eph}: inlined as CTEs, no physical table to test directly")
    seeds = list(manifest.get("seeds", {}).keys())
    if seeds:
        out.append(f"dbt seeds present ({len(seeds)}): reference data, check key uniqueness")
    return out


def parse_dbt_files(files: dict[str, str], label: str | None = None):
    """Best-effort parsing when only .sql model files are available (no manifest).

    Uses the SQL parser per model and stitches lineage by dbt `ref()`/`source()`.
    """
    from app.parsers.sql_parser import parse_sql
    from app.schemas import EtlAnalysis

    manifest = next((json.loads(v) for v in files.values() if v.strip().startswith("{")
                     and "nodes" in v), None)
    if manifest:
        return parse_dbt_manifest(manifest, label=label)

    transformations: list[Transformation] = []
    edges: list[LineageEdge] = []
    sources: list[str] = []
    targets: list[str] = []
    risks: list[str] = []
    external: list[str] = []
    primary_by_model: dict[str, str] = {}

    model_paths = {p: c for p, c in files.items() if p.lower().endswith(".sql")}
    # a ref that no model produces must be a physical table in some database
    built = {os.path.splitext(os.path.basename(p))[0].lower() for p in model_paths}
    seeds = {
        os.path.splitext(os.path.basename(p))[0].lower()
        for p in files
        if p.lower().endswith(".csv") and "seed" in p.lower().replace("/", " ")
    }

    for path, code in model_paths.items():
        model = os.path.splitext(os.path.basename(path))[0]
        targets.append(model)
        analysis = parse_sql(code, label=model)

        for ref in _dbt_refs(code):
            edges.append(LineageEdge(source=ref, target=model))
            transformations.append(
                Transformation(
                    kind="ref", description=f"{{{{ ref('{ref}') }}}} in {model}",
                    source=ref, target=model, confidence=1.0,
                )
            )
            # the first table a model reads is the one that sets its grain
            if model not in primary_by_model:
                primary_by_model[model] = ref
            low = ref.lower()
            if low not in built:
                external.append(ref)
                if low in seeds:
                    transformations.append(
                        Transformation(
                            kind="seed",
                            description=f"'{ref}' is a dbt seed (a CSV loaded as a table)",
                            source=ref,
                            confidence=1.0,
                        )
                    )
        for src in _dbt_sources(code):
            full = f"{src[0]}.{src[1]}"
            sources.append(full)
            edges.append(LineageEdge(source=full, target=model))
            if model not in primary_by_model:
                primary_by_model[model] = full
            transformations.append(
                Transformation(
                    kind="source", description=f"{{{{ source('{src[0]}','{src[1]}') }}}} in {model}",
                    source=full, target=model, confidence=1.0,
                )
            )
        transformations.extend(
            Transformation(kind=t.kind, description=f"[{model}] {t.description}", target=model)
            for t in analysis.transformations
        )
        risks.extend(analysis.risks)

    if external:
        sources.extend(_dedupe(external))
        risks.append(
            f"{len(external)} referenced table(s) are not built by any model in this "
            f"codebase ({', '.join(sorted(set(external))[:5])}) — they must already "
            "exist in a database, so I'll ask for that connection."
        )

    if not transformations:
        risks.append("No dbt models or manifest detected in the provided files")

    return EtlAnalysis(
        language=EtlLanguage.DBT,
        label=label,
        transformations=transformations,
        lineage=edges,
        sources=_dedupe(sources),
        targets=_dedupe(targets),
        primary_sources=_dedupe(list(primary_by_model.values())),
        grain=dict(primary_by_model),
        risks=_dedupe(risks),
        raw_code="",
    )


def _dbt_refs(code: str) -> list[str]:
    import re

    return re.findall(r"\{\{\s*ref\(\s*['\"]([\w.]+)['\"]", code)


def _dbt_sources(code: str) -> list[tuple[str, str]]:
    import re

    return [
        (m[0], m[1])
        for m in re.findall(r"\{\{\s*source\(\s*['\"]([\w.]+)['\"]\s*,\s*['\"]([\w.]+)['\"]", code)
    ]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for i in items:
        if i and i not in seen:
            seen.add(i)
            out.append(i)
    return out


__all__ = ["parse_dbt_manifest", "parse_dbt_files"]

def dbt_hints(files: dict[str, str]) -> tuple[dict[str, dict], list[dict]]:
    """Extract per-table database/schema + connection hints from dbt YAML.

    `profiles.yml` names the warehouse, and `sources:` blocks map logical
    `source('raw','orders')` references to physical database/schema/identifier —
    which is exactly what lets us group tables per database with high confidence.
    """
    table_hints: dict[str, dict] = {}
    conn_hints: list[dict] = []

    profile_text = next((v for k, v in files.items() if k.endswith("profiles.yml")), None)
    if profile_text:
        try:
            prof = yaml.safe_load(profile_text) or {}
        except yaml.YAMLError:
            prof = {}
        for _name, cfg in (prof or {}).items():
            outputs = (cfg or {}).get("outputs") or {}
            for _target, o in outputs.items():
                if not isinstance(o, dict):
                    continue
                # profiles.yml is Jinja: recover env_var defaults, never a
                # raw `{{ ... }}` string, and never a password reference
                conn_hints.append(
                    {
                        "type": str(jinja_value(o.get("type")) or ""),
                        "host": jinja_value(o.get("host")),
                        "port": coerce_port(o.get("port")),
                        "database": jinja_value(o.get("dbname") or o.get("database")),
                        "schema": jinja_value(o.get("schema")),
                        "username": jinja_value(o.get("user") or o.get("username")),
                        "account": jinja_value(o.get("account")),
                        "warehouse": jinja_value(o.get("warehouse")),
                    }
                )

    for path, text in files.items():
        if not path.endswith((".yml", ".yaml")) or path.endswith("profiles.yml"):
            continue
        try:
            doc = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(doc, dict):
            continue
        for src in doc.get("sources") or []:
            if not isinstance(src, dict):
                continue
            sname = jinja_value(src.get("name"))
            base_db = jinja_value(src.get("database"))
            base_schema = jinja_value(src.get("schema") or src.get("identifier"))
            for tbl in src.get("tables") or []:
                if not isinstance(tbl, dict) or not sname:
                    continue
                logical = f"{sname}.{jinja_value(tbl.get('name'))}"
                hint = {
                    "database": jinja_value(tbl.get("database")) or base_db,
                    "schema": jinja_value(tbl.get("schema")) or base_schema,
                    "table": jinja_value(tbl.get("identifier")) or jinja_value(tbl.get("name")),
                    "origin": "dbt sources.yml",
                }
                table_hints[logical] = hint
                # also index by the physical name so `raw.orders` in SQL binds too
                if base_schema:
                    table_hints.setdefault(f"{base_schema}.{jinja_value(tbl.get('name'))}", hint)

    # keep hints honest: drop empty rows and never carry a password forward
    conn_hints = [
        {k: v for k, v in h.items() if v not in (None, "", "password", "pass")}
        for h in conn_hints
        if any(h.get(k) for k in ("type", "database", "host", "account"))
    ]
    return table_hints, conn_hints