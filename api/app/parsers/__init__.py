"""Unified ETL parsing entrypoint.

`analyze_etl` is the only function callers should use. It is deliberately
language-aware in two ways:

* A single script (paste) is parsed with exactly one parser.
* A codebase (repo / upload) is usually mixed — dbt models alongside Python
  jobs — so each file is parsed with its own parser and the results are merged.
  Picking one language for a repo silently loses half the lineage.

The entrypoint also attaches provenance and derives `required_sources`, so every
path (chat, REST, worker) ends up with an analysis the agent can plan from.
"""

from __future__ import annotations

from app.parsers.dbt_parser import dbt_hints, parse_dbt_files, parse_dbt_manifest
from app.parsers.python_parser import parse_python
from app.parsers.sql_parser import parse_sql
from app.schemas import (
    EtlAnalysis,
    EtlLanguage,
    EtlOrigin,
    EtlSubmit,
)
from app.services.requirements import annotate


def _parse_one(name: str, text: str) -> EtlAnalysis | None:
    lower = name.lower()
    if lower.endswith((".sql", ".ddl")):
        return parse_sql(text, label=name)
    if lower.endswith(".py"):
        return parse_python(text, label=name)
    return None


def _merge(parts: list[EtlAnalysis], language: EtlLanguage, label: str | None) -> EtlAnalysis:
    """Union sources/targets/lineage across every file in the project."""
    merged = EtlAnalysis(language=language, label=label)
    seen_t: set[tuple] = set()
    seen_e: set[tuple] = set()

    for part in parts:
        for t in part.transformations:
            key = (t.kind, t.description, t.target)
            if key not in seen_t:
                seen_t.add(key)
                merged.transformations.append(t)
        for e in part.lineage:
            key = (e.source, e.target)
            if key not in seen_e:
                seen_e.add(key)
                merged.lineage.append(e)
        for bucket, field in (
            (part.sources, "sources"),
            (part.targets, "targets"),
            (part.joins, "joins"),
            (part.filters, "filters"),
            (part.aggregations, "aggregations"),
            (part.primary_sources, "primary_sources"),
            (part.risks, "risks"),
        ):
            dst = getattr(merged, field)
            for item in bucket:
                if item not in dst:
                    dst.append(item)
    return merged


def _project_language(submit: EtlSubmit, files: dict[str, str]) -> EtlLanguage:
    """Prefer the declared language unless the tree clearly says otherwise."""
    names = [n.lower() for n in files]
    if any(n.endswith("dbt_project.yml") for n in names) or submit.language == EtlLanguage.DBT:
        return EtlLanguage.DBT
    has_py = any(n.endswith(".py") for n in names)
    has_sql = any(n.endswith((".sql", ".ddl")) for n in names)
    if has_py and not has_sql:
        return EtlLanguage.PYTHON
    return EtlLanguage.SQL


def analyze_etl(submit: EtlSubmit) -> EtlAnalysis:
    files = dict(submit.files or {})
    origin = (
        EtlOrigin(submit.origin)
        if submit.origin
        else (EtlOrigin.PASTE if submit.code else EtlOrigin.UPLOAD if files else EtlOrigin.UNKNOWN)
    )

    if files or submit.manifest:
        language = _project_language(submit, files)
        if language == EtlLanguage.DBT:
            analysis = (
                parse_dbt_manifest(submit.manifest or {}, label=submit.label)
                if submit.manifest
                else parse_dbt_files(files, label=submit.label)
            )
            th, ch = dbt_hints(files)
            for k, v in th.items():
                analysis.table_hints.setdefault(k, v)
            if ch:
                analysis.connection_hints.extend(ch)
            # dbt models are covered by the parser above; fold in any sibling
            # python jobs / loose .sql that speaks about other tables
            siblings = [
                a
                for a in (_parse_one(n, t) for n, t in files.items() if n.lower().endswith(".py"))
                if a
            ]
            if siblings:
                analysis = _merge([analysis, *siblings], language, submit.label)
        else:
            parts = [a for a in (_parse_one(n, t) for n, t in files.items()) if a]
            if not parts:
                code = submit.code or "\n".join(files.values())
                parts = [
                    parse_python(code, label=submit.label)
                    if language == EtlLanguage.PYTHON
                    else parse_sql(code, label=submit.label)
                ]
            analysis = _merge(parts, language, submit.label)

        # dbt YAML is the richest source of database/schema truth
        hints, conn_hints = dbt_hints(files)
        for k, v in hints.items():
            analysis.table_hints.setdefault(k, v)
        if conn_hints:
            analysis.connection_hints = conn_hints
    else:
        code = submit.code or ""
        match submit.language:
            case "python":
                analysis = parse_python(code, label=submit.label)
            case "dbt":
                analysis = parse_dbt_files({"models/model.sql": code}, label=submit.label)
            case _:
                analysis = parse_sql(code, label=submit.label)

    analysis.origin = origin
    analysis.repo_url = submit.repo_url
    analysis.repo_ref = submit.repo_ref
    analysis.files = sorted(files) or (["<pasted>"] if submit.code else [])
    # data-file profiles travel with the analysis; they are evidence about
    # tables, not source, so they never enter the parsers or the blob
    analysis.data_profiles = list(submit.data_profiles or [])
    if submit.code and not analysis.raw_code:
        analysis.raw_code = submit.code

    return annotate(analysis, code_blob=submit.code or "\n".join(files.values()))


__all__ = [
    "analyze_etl",
    "parse_sql",
    "parse_python",
    "parse_dbt_files",
    "parse_dbt_manifest",
    "dbt_hints",
]
