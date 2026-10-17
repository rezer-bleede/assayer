"""Tests for the shared intake filter (upload + git clone must agree)."""

from __future__ import annotations

import json

from app.services.source_filter import (
    Kind,
    Skip,
    assemble_sources,
    classify,
    is_seed_csv,
    looks_binary,
    looks_generated,
    looks_like_dump,
    sniff_json,
)

SQL = b"SELECT 1 FROM raw.orders"


def _items(*pairs: tuple[str, bytes]) -> list[tuple[str, bytes]]:
    return list(pairs)


def _kind(rel: str, **kw) -> Kind:
    return classify(rel, size=kw.pop("size", 10), **kw).kind


# --- classify: routing ----------------------------------------------------- #


def test_classify_routes_source_types() -> None:
    for rel in [
        "models/orders.sql",
        "jobs/build.py",
        "dbt_project.yml",
        "profiles.yml",
        "target/manifest.json",
        "models/schema.yml",
        "models/macros/date.sql",
    ]:
        assert _kind(rel, max_file_bytes=1000) in (Kind.CODE, Kind.CONFIG), rel

    assert _kind("models/orders.sql", max_file_bytes=1000) is Kind.CODE
    assert _kind("models/schema.yml", max_file_bytes=1000) is Kind.CONFIG
    assert _kind("seeds/raw_orders.csv", max_file_bytes=1000) is Kind.DATA
    assert _kind("data/dump.jsonl", max_file_bytes=1000) is Kind.DATA
    assert _kind("warehouse/events.parquet", max_file_bytes=1000) is Kind.DATA


def test_classify_skips_dependency_and_vcs_dirs() -> None:
    for rel in [
        ".git/objects/ab/cdef",
        "myproj/node_modules/pkg/index.js",
        "dbt/.venv/lib/site.py",
        "x/__pycache__/y.py",
        ".hg/hgrc",
        "logs/run.sql",
    ]:
        assert classify(rel, size=10, max_file_bytes=1000).reason is Skip.DIR, rel


def test_classify_skips_irrelevant_types() -> None:
    for rel in ["img/logo.png", "big.dump", "notes.md", "setup.cfg"]:
        assert classify(rel, size=10, max_file_bytes=1000).reason is Skip.TYPE, rel


def test_classify_drops_txt_and_lockfiles() -> None:
    assert classify("docs/notes.txt", size=10, max_file_bytes=1000).reason is Skip.TYPE
    assert classify("package-lock.json", size=10, max_file_bytes=1000).reason is Skip.JUNK
    assert classify("pnpm-lock.yaml", size=10, max_file_bytes=1000).reason is Skip.JUNK
    assert classify("shop/target/catalog.json", size=10, max_file_bytes=10**6).reason in (
        Skip.JUNK,
        Skip.TARGET_NOISE,
    )


def test_parquet_is_not_mistaken_for_a_binary_blob() -> None:
    raw = b"PAR1\x00\x00\x00\x00\x01\x02"
    intake = assemble_sources(
        _items(("warehouse/customers.parquet", raw), ("m.sql", SQL)),
        max_files=50,
        max_file_bytes=10_000,
    )
    assert len(intake.profiles) == 1
    assert intake.profiles[0].format == "parquet"


def test_generic_json_needs_content() -> None:
    assert _kind("data.json", max_file_bytes=1000) is Kind.SNIFF
    # the manifest is lineage by name, never guessed at
    assert _kind("target/manifest.json", max_file_bytes=1000) is Kind.CONFIG


def test_classify_skips_oversized_files() -> None:
    assert classify("models/a.sql", size=5000, max_file_bytes=1000).reason is Skip.SIZE


def test_data_files_get_their_own_cap() -> None:
    csv = "seeds/raw.csv"
    assert classify(csv, size=5000, max_file_bytes=1000, data_max_bytes=100_000).kind is Kind.DATA
    assert classify(csv, size=500_000, max_file_bytes=1000, data_max_bytes=100_000).reason is (
        Skip.SIZE
    )


def test_classify_target_noise_but_not_manifest() -> None:
    assert classify("target/compiled/m/x.sql", size=10, max_file_bytes=1000).reason is (
        Skip.TARGET_NOISE
    )
    assert classify("target/manifest.json", size=10, max_file_bytes=1000).kind is Kind.CONFIG


def test_manifest_gets_a_larger_budget() -> None:
    big = 3_000_000
    assert (
        classify(
            "target/manifest.json", size=big, max_file_bytes=1000, manifest_max_bytes=4_000_000
        ).kind
        is Kind.CONFIG
    )
    assert classify("models/x.sql", size=big, max_file_bytes=1000).reason is Skip.SIZE


def test_trust_mode_accepts_anything_textual() -> None:
    """A file the user named by hand is taken at face value."""
    assert _kind("scratch/notes.txt", max_file_bytes=1000, trust=True) is Kind.CODE
    assert _kind("package-lock.json", max_file_bytes=1000, trust=True) is Kind.CODE
    assert _kind("dump.json", max_file_bytes=1000, trust=True) is Kind.CODE


# --- seeds / content sniffs ------------------------------------------------- #


def test_seed_csv_recognised() -> None:
    assert is_seed_csv("myproj/seeds/raw_orders.csv")
    assert is_seed_csv("seeds/accounts.csv")
    assert is_seed_csv("seeds/seed_countries.csv")
    assert not is_seed_csv("data/orders.csv")
    assert not is_seed_csv("seeds/notes.txt")
    # `seedlings` is data, not a seed: the segment test is anchored
    assert not is_seed_csv("research/seedlings.csv")


def test_looks_binary() -> None:
    assert looks_binary(b"PK\x03\x04\x00\x00png stuff\x00")
    assert not looks_binary(SQL)


def test_looks_generated() -> None:
    assert looks_generated("x" * 20_000)
    assert looks_generated("\n".join("y" * 900 for _ in range(20)))
    assert not looks_generated("select 1\nselect 2\n")


def test_looks_like_dump() -> None:
    rows = "\n".join(f"INSERT INTO t VALUES ({i}, 'a', 'b')," for i in range(400))
    assert looks_like_dump(rows)
    assert not looks_like_dump("select * from raw.orders")


def test_sniff_json_routes_config_and_data() -> None:
    assert sniff_json('[{"id": 1}]') is Kind.DATA
    assert sniff_json('{"data": [{"id": 1}]}') is Kind.DATA
    assert sniff_json('{"name": "shop", "models": {"m": {}}}') is Kind.CONFIG
    assert sniff_json('{"version": 2, "profile": "prod"}') is Kind.CONFIG
    assert sniff_json("not json at all") is Kind.SKIP


# --- assemble_sources ------------------------------------------------------- #


def test_assemble_keeps_relevant_skips_junk_with_note() -> None:
    items = _items(
        ("proj/models/orders.sql", SQL),
        ("proj/.git/objects/xx", b"junk"),
        ("proj/node_modules/pkg/index.js", b"var x"),
        ("proj/img/logo.png", b"\x89PNG\x00\x00"),
        ("proj/data/events.parquet", b"coldata"),
    )
    intake = assemble_sources(items, max_files=50, max_file_bytes=10_000)
    assert list(intake.contents) == ["proj/models/orders.sql"]
    assert intake.manifest is None
    assert intake.notes and "Ignored" in intake.notes[0]
    assert ".git" in intake.notes[0] or "VCS" in intake.notes[0] or "metadata" in intake.notes[0].lower()


def test_assemble_profiles_seeds_without_reading_the_body() -> None:
    csv = b"id,amount\n" + b"".join(f"{i},{i * 2}\n".encode() for i in range(200))
    items = _items(
        ("proj/seeds/raw_orders.csv", csv),
        ("proj/models/stg_orders.sql", SQL),
    )
    intake = assemble_sources(items, max_files=50, max_file_bytes=100, data_max_bytes=10_000)
    assert intake.contents["proj/seeds/raw_orders.csv"] == ""   # name only
    assert intake.contents["proj/models/stg_orders.sql"] == SQL.decode()
    assert len(intake.profiles) == 1
    profile = intake.profiles[0]
    assert profile.table == "raw_orders"
    assert profile.row_count == 200
    assert [c.name for c in profile.columns] == ["id", "amount"]
    assert profile.columns[0].data_type == "integer"
    # the rows themselves never become source
    assert "id,amount" not in intake.blob


def test_non_seed_data_file_is_profiled_but_not_parsed() -> None:
    items = _items(
        ("proj/data/customers.csv", b"id,email\n1,a@b.co\n"),
        ("proj/models/orders.sql", SQL),
    )
    intake = assemble_sources(items, max_files=50, max_file_bytes=10_000)
    assert "proj/data/customers.csv" not in intake.contents
    assert [p.table for p in intake.profiles] == ["customers"]


def test_json_array_is_profiled_json_config_is_parsed() -> None:
    items = _items(
        ("proj/export/rows.json", b'[{"id": 1, "name": "a"}]'),
        ("proj/config.json", b'{"name": "shop", "profile": "prod"}'),
        ("proj/models/orders.sql", SQL),
    )
    intake = assemble_sources(items, max_files=50, max_file_bytes=10_000)
    assert [p.table for p in intake.profiles] == ["rows"]
    assert intake.contents["proj/config.json"] == b'{"name": "shop", "profile": "prod"}'.decode()


def test_big_json_is_profiled_not_parsed() -> None:
    payload = json.dumps([{"id": i, "blob": "x" * 50} for i in range(900)])
    assert len(payload) > 32_000
    intake = assemble_sources(
        _items(("data/big.json", payload.encode()), ("m.sql", SQL)),
        max_files=50,
        max_file_bytes=131_072,
    )
    assert "data/big.json" not in intake.contents
    assert [p.row_count for p in intake.profiles] == [900]


def test_generated_and_dump_files_are_dropped_with_a_reason() -> None:
    items = _items(
        ("models/bundle.sql", (b"x" * 600 + b"\n") * 10),
        ("models/rows.sql", b"\n".join(f"INSERT INTO t VALUES ({i});".encode() for i in range(400))),
        ("models/good.sql", SQL),
    )
    intake = assemble_sources(items, max_files=50, max_file_bytes=100_000)
    assert list(intake.contents) == ["models/good.sql"]
    assert "generated" in intake.notes[0]
    assert "bulk data" in intake.notes[0]


def test_total_budget_stops_source_reading() -> None:
    items = _items(*[(f"m/{i}.sql", b"select 1;\n" * 40) for i in range(20)])
    intake = assemble_sources(
        items, max_files=50, max_file_bytes=10_000, max_total_bytes=200
    )
    assert len(intake.contents) < 20
    assert "budget" in intake.notes[0]


def test_trust_mode_keeps_a_picked_file_the_filter_would_drop() -> None:
    items = _items(("dump.json", b'[{"id": 1}]'))
    strict = assemble_sources(items, max_files=50, max_file_bytes=10_000)
    assert strict.contents == {} and len(strict.profiles) == 1

    trusted = assemble_sources(items, max_files=50, max_file_bytes=10_000, trust=True)
    assert trusted.contents["dump.json"] == '[{"id": 1}]'
    assert trusted.profiles == []


def test_assemble_extracts_dbt_manifest() -> None:
    manifest_doc = {"nodes": {"model.a.b": {}}, "sources": {}}
    items = _items(("proj/target/manifest.json", json.dumps(manifest_doc).encode()))
    intake = assemble_sources(items, max_files=50, max_file_bytes=10**9)
    assert intake.manifest == manifest_doc


def test_assemble_manifest_with_no_nodes_is_stored_as_plain_json() -> None:
    items = _items(("config/manifest.json", b'{"not": "dbt"}'))
    intake = assemble_sources(items, max_files=50, max_file_bytes=10**9)
    assert intake.manifest is None
    assert "config/manifest.json" in intake.contents


def test_assemble_respects_max_files() -> None:
    items = _items(*[(f"m/{i}.sql", SQL) for i in range(10)])
    intake = assemble_sources(items, max_files=3, max_file_bytes=10_000)
    assert len(intake.contents) == 3
    assert any("count cap" in n for n in intake.notes)


def test_assemble_binary_names_as_source_are_dropped() -> None:
    items = _items(("models/blob.sql", bytes(range(1, 40)) * 30))
    intake = assemble_sources(items, max_files=50, max_file_bytes=10_000)
    assert intake.contents == {}
    assert any("binary" in n for n in intake.notes)


def test_assemble_no_notes_when_nothing_filtered() -> None:
    items = _items(("models/a.sql", SQL))
    intake = assemble_sources(items, max_files=50, max_file_bytes=10_000)
    assert intake.notes == []


def test_profile_summary_reads_like_a_sentence() -> None:
    items = _items(("seeds/raw_orders.csv", b"id\n1\n2\n"))
    intake = assemble_sources(items, max_files=50, max_file_bytes=100, data_max_bytes=10_000)
    from app.services.source_filter import profile_summary

    summary = profile_summary(intake.profiles)
    assert summary.startswith("Profiled 1 data file(s)")
    assert "raw_orders.csv" in summary and "2 row(s)" in summary


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
        "shop/seeds/raw_customers.csv": "",
    }
    analysis = analyze_etl(EtlSubmit(language="dbt", files=files, origin="upload"))
    assert "raw_customers" in analysis.sources or any(
        "raw_customers" in t for r in analysis.required_sources for t in r.tables
    )


def test_nested_target_dir_still_noise() -> None:
    """A folder upload prefixes paths with the project dir; target rules hold."""
    assert (
        classify("shop/target/compiled/m/orders.sql", size=10, max_file_bytes=1000).reason
        is Skip.TARGET_NOISE
    )
    assert classify("shop/target/manifest.json", size=10, max_file_bytes=1000).kind is Kind.CONFIG