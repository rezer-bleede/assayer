"""Tests for the shared source-relevance filter (upload + git intake)."""

from __future__ import annotations

import json

from app.services.source_filter import (
    Skip,
    assemble_sources,
    classify,
    is_seed_csv,
    looks_binary,
)

SQL = b"SELECT 1 FROM raw.orders"


def _items(*pairs: tuple[str, bytes]) -> list[tuple[str, bytes]]:
    return list(pairs)


# --- classify ------------------------------------------------------------ #


def test_classify_keeps_source_types() -> None:
    for rel in [
        "models/orders.sql",
        "jobs/build.py",
        "dbt_project.yml",
        "profiles.yml",
        "target/manifest.json",
        "models/schema.yml",
    ]:
        assert classify(rel, size=10, max_file_bytes=1000) is Skip.KEEP, rel


def test_classify_skips_dependency_and_vcs_dirs() -> None:
    for rel in [
        ".git/objects/ab/cdef",
        "myproj/node_modules/pkg/index.js",
        "dbt/.venv/lib/site.py",
        "x/__pycache__/y.py",
        ".hg/hgrc",
        "logs/run.sql",
    ]:
        assert classify(rel, size=10, max_file_bytes=1000) is Skip.DIR, rel


def test_classify_skips_irrelevant_types() -> None:
    for rel in ["data/events.parquet", "img/logo.png", "big.dump", "notes.md", "x.csv"]:
        assert classify(rel, size=10, max_file_bytes=1000) is Skip.TYPE, rel


def test_classify_skips_oversized_files() -> None:
    assert classify("models/a.sql", size=5000, max_file_bytes=1000) is Skip.SIZE


def test_classify_target_noise_but_not_manifest() -> None:
    assert classify("target/compiled/m/x.sql", size=10, max_file_bytes=1000) is Skip.TARGET_NOISE
    assert classify("target/manifest.json", size=10, max_file_bytes=1000) is Skip.KEEP


def test_manifest_gets_a_larger_budget() -> None:
    big = 3_000_000
    assert classify("target/manifest.json", size=big, max_file_bytes=1000,
                    manifest_max_bytes=4_000_000) is Skip.KEEP
    assert classify("models/x.sql", size=big, max_file_bytes=1000) is Skip.SIZE


# --- seeds / binary sniff -------------------------------------------------- #


def test_seed_csv_recognised() -> None:
    assert is_seed_csv("myproj/seeds/raw_orders.csv")
    assert is_seed_csv("seeds/accounts.csv")
    assert not is_seed_csv("data/orders.csv")
    assert not is_seed_csv("seeds/notes.txt")


def test_looks_binary() -> None:
    assert looks_binary(b"PK\x03\x04\x00\x00png stuff\x00")
    assert not looks_binary(SQL)


# --- assemble_sources ------------------------------------------------------ #


def test_assemble_keeps_relevant_skips_junk_with_note() -> None:
    items = _items(
        ("proj/models/orders.sql", SQL),
        ("proj/.git/objects/xx", b"junk"),
        ("proj/node_modules/pkg/index.js", b"var x"),
        ("proj/img/logo.png", b"\x89PNG\x00\x00"),
        ("proj/data/events.parquet", b"coldata"),
    )
    contents, manifest, notes = assemble_sources(items, max_files=50, max_file_bytes=10_000)
    assert list(contents) == ["proj/models/orders.sql"]
    assert manifest is None
    assert notes and "Ignored" in notes[0]
    assert ".git" in notes[0] or "VCS" in notes[0] or "metadata" in notes[0].lower()


def test_assemble_registers_seed_csvs_without_reading_body() -> None:
    items = _items(
        ("proj/seeds/raw_orders.csv", b"a,b,c\n" * 10_000),
        ("proj/models/stg_orders.sql", SQL),
    )
    contents, _, _ = assemble_sources(items, max_files=50, max_file_bytes=100)
    assert contents["proj/seeds/raw_orders.csv"] == ""   # name only, body dropped
    assert contents["proj/models/stg_orders.sql"] == SQL.decode()


def test_assemble_extracts_dbt_manifest() -> None:
    manifest_doc = {"nodes": {"model.a.b": {}}, "sources": {}}
    items = _items(("proj/target/manifest.json", json.dumps(manifest_doc).encode()))
    _, manifest, _ = assemble_sources(items, max_files=50, max_file_bytes=10**9)
    assert manifest == manifest_doc


def test_assemble_manifest_with_no_nodes_is_stored_as_plain_json() -> None:
    items = _items(("config/manifest.json", b'{"not": "dbt"}'))
    contents, manifest, _ = assemble_sources(items, max_files=50, max_file_bytes=10**9)
    assert manifest is None
    assert "config/manifest.json" in contents


def test_assemble_respects_max_files() -> None:
    items = _items(*[(f"m/{i}.sql", SQL) for i in range(10)])
    contents, _, notes = assemble_sources(items, max_files=3, max_file_bytes=10_000)
    assert len(contents) == 3
    assert any("count cap" in n for n in notes)


def test_assemble_binary_names_as_source_are_dropped() -> None:
    items = _items(("models/blob.sql", bytes(range(1, 40)) * 30))
    contents, _, notes = assemble_sources(items, max_files=50, max_file_bytes=10_000)
    assert contents == {}
    assert any("binary" in n for n in notes)


def test_assemble_no_notes_when_nothing_filtered() -> None:
    items = _items(("models/a.sql", SQL))
    _, _, notes = assemble_sources(items, max_files=50, max_file_bytes=10_000)
    assert notes == []


# --- end-to-end through the upload route shape ------------------------------ #


def test_folder_upload_parses_as_dbt_project() -> None:
    """A folder upload keyed by relative path still parses as a dbt project."""
    from app.parsers import analyze_etl
    from app.schemas import EtlSubmit

    files = {
        "shop/dbt_project.yml": "name: shop\n",
        "shop/models/stg_orders.sql": "select * from {{ source('raw','orders') }}",
        "shop/models/orders.sql": (
            "select * from {{ ref('stg_orders') }} o join {{ ref('raw_customers') }} c on 1=1"
        ),
        "shop/seeds/raw_customers.csv": "id,name\n1,a\n",
    }
    analysis = analyze_etl(EtlSubmit(language="dbt", files=files, origin="upload"))
    # seed referenced but not built -> listed as required source
    assert "raw_customers" in analysis.sources or any(
        "raw_customers" in t for r in analysis.required_sources for t in r.tables
    )


def test_nested_target_dir_still_noise() -> None:
    """A folder upload prefixes paths with the project dir; target rules hold."""
    assert (
        classify("shop/target/compiled/m/orders.sql", size=10, max_file_bytes=1000)
        is Skip.TARGET_NOISE
    )
    assert classify("shop/target/manifest.json", size=10, max_file_bytes=1000) is Skip.KEEP
