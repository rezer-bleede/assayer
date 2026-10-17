"""Tests for data-file profiling: what we learn without reading the rows."""

from __future__ import annotations

import json

from app.services.profile import infer_table_name, profile_file


def _csv(header: str, rows: list[str]) -> bytes:
    return (header + "\n" + "\n".join(rows) + "\n").encode()


def test_csv_types_rows_and_samples() -> None:
    raw = _csv(
        "id,amount,email,signup_date,active",
        [
            "1,10.50,a@example.com,2024-01-05,true",
            "2,20.00,b@example.com,2024-02-06,false",
            "3,,c@example.com,2024-03-07,true",
        ],
    )
    profile = profile_file("seeds/raw_customers.csv", raw)
    assert profile is not None
    assert profile.table == "raw_customers"
    assert profile.format == "csv"
    assert profile.row_count == 3
    assert profile.sampled_rows == 3
    types = {c.name: c.data_type for c in profile.columns}
    assert types == {
        "id": "integer",
        "amount": "number",
        "email": "string",
        "signup_date": "date",
        "active": "boolean",
    }
    amount = next(c for c in profile.columns if c.name == "amount")
    assert amount.samples == ["10.50", "20.00"]
    assert amount.null_ratio == 0.3333
    assert profile.columns[2].distinct_values == 3


def test_csv_nulls_are_counted_and_blank_headers_named() -> None:
    raw = b"id,,name\n1,,a\n2,,\n3,NA,\n"
    profile = profile_file("d/x.csv", raw)
    assert profile is not None
    names = [c.name for c in profile.columns]
    assert names == ["id", "column", "name"]
    unnamed = profile.columns[1]
    assert unnamed.null_ratio == 1.0
    assert profile.columns[2].null_ratio == 0.6667


def test_tsv_delimiter_is_sniffed() -> None:
    raw = b"id\tname\n1\ta\n"
    profile = profile_file("d/x.tsv", raw)
    assert profile is not None
    assert [c.name for c in profile.columns] == ["id", "name"]


def test_semicolon_csv_is_sniffed() -> None:
    raw = b"id;name;city\n1;a;Zurich\n"
    profile = profile_file("d/x.csv", raw)
    assert profile is not None
    assert [c.name for c in profile.columns] == ["id", "name", "city"]


def test_csv_row_count_is_exact_beyond_the_sample_window() -> None:
    rows = [f"{i},x" for i in range(5_000)]
    profile = profile_file("d/big.csv", _csv("id,val", rows))
    assert profile is not None
    assert profile.row_count == 5_000
    assert profile.sampled_rows == 2_000
    assert profile.truncated is True


def test_json_array_profiled_by_key_union() -> None:
    raw = json.dumps(
        [
            {"id": 1, "name": "a"},
            {"id": 2, "name": "b", "extra": True},
            {"id": None},
        ]
    ).encode()
    profile = profile_file("export/orders.json", raw)
    assert profile is not None
    assert profile.format == "json"
    assert profile.table == "orders"
    assert profile.row_count == 3
    assert [c.name for c in profile.columns] == ["id", "name", "extra"]
    assert profile.columns[0].null_ratio == 0.3333
    assert profile.columns[1].data_type == "string"


def test_json_data_wrapper_is_unwrapped() -> None:
    raw = json.dumps({"data": [{"id": 1}, {"id": 2}]}).encode()
    profile = profile_file("export/x.json", raw)
    assert profile is not None
    assert profile.row_count == 2


def test_jsonl_profiled_line_by_line() -> None:
    raw = b'{"id": 1}\n{"id": 2}\n\n{"id": 3}\n'
    profile = profile_file("export/events.jsonl", raw)
    assert profile is not None
    assert profile.format == "jsonl"
    assert profile.row_count == 3
    assert profile.columns[0].name == "id"


def test_json_keys_that_need_sanitising_keep_their_values() -> None:
    """`user id` is reported as `user_id`, and the values must follow it."""
    raw = json.dumps([{"user id": 1, "\ufeffname": "a"}, {"user id": 2, "\ufeffname": "b"}]).encode()
    profile = profile_file("export/x.json", raw)
    assert profile is not None
    cols = {c.name: c for c in profile.columns}
    assert set(cols) == {"user_id", "name"}
    assert cols["user_id"].data_type == "integer"
    assert cols["user_id"].null_ratio == 0.0
    assert cols["user_id"].samples == ["1", "2"]


def test_jsonl_columns_missing_from_some_rows_are_reported() -> None:
    profile = profile_file("d/x.jsonl", b'{"id": 1, "note": "a"}\n{"id": 2}\n')
    assert profile is not None
    note = next(c for c in profile.columns if c.name == "note")
    assert note.null_ratio == 0.5


def test_non_tabular_json_returns_nothing() -> None:
    assert profile_file("config.json", b'{"name": "shop"}') is None
    assert profile_file("notes.txt", b"hello") is None


def test_table_name_inference_sanitises() -> None:
    assert infer_table_name("seeds/Raw Orders-2024.csv") == "raw_orders_2024"
    assert infer_table_name("a/b/my.table.v2.jsonl") == "my_table_v2"


def test_parquet_profiles_through_duckdb() -> None:
    duckdb = __import__("pytest").importorskip("duckdb")
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "events.parquet"
        con = duckdb.connect()
        con.execute(
            "COPY (SELECT * FROM (VALUES (1, 'a'), (2, 'b')) t(id, name)) TO ? (FORMAT PARQUET)",
            [str(path)],
        )
        con.close()
        raw = path.read_bytes()

    profile = profile_file("warehouse/events.parquet", raw)
    assert profile is not None
    assert profile.format == "parquet"
    assert profile.row_count == 2
    assert [c.name for c in profile.columns] == ["id", "name"]
    assert profile.columns[1].samples == ["a", "b"]


def test_truncated_scan_is_flagged_not_hidden() -> None:
    raw = _csv("id,val", [f"{i},x" for i in range(20)]) + b"9" * 4_100_000
    profile = profile_file("d/huge.csv", raw)
    assert profile is not None
    assert profile.truncated is True
    assert profile.note and "scanned the first" in profile.note