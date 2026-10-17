"""Profile data files into column metadata instead of reading them as code.

A dbt seed, a raw CSV drop or an exported JSON dump is not lineage — it is
*evidence*. Knowing that ``seeds/raw_customers.csv`` carries 40k rows and the
columns ``id, email, signup_date`` lets the agent check that table once it is
connected, compare the file against the warehouse schema, and explain grain —
without a single row of the file entering the model context or the LLM prompt.

Everything here is streaming or bounded:

* csv/tsv  — `csv.reader` over the decoded text, stats from the first rows.
* json     — arrays of objects (or ``{"data": [...]}`` wrappers); keys are the
  union of the first records, so a heterogeneous export degrades gracefully.
* jsonl    — one object per line.
* parquet  — duckdb, if installed; otherwise the file is registered by name
  with an explicit note rather than silently dropped.

Only the profile (columns, inferred types, row count, a handful of sample
values) leaves this module. The rows themselves do not.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import re
import tempfile
from pathlib import Path
from typing import Any

from app.core.logging import log
from app.schemas import ColumnProfile, DataProfile

# stats are computed from the head of the file; the row count is exact
MAX_SAMPLE_ROWS = 2_000
MAX_SAMPLE_VALUES = 5
MAX_COLUMNS = 200
MAX_SCAN_BYTES = 4_000_000   # bytes actually parsed; the rest sets `truncated`
MAX_SAMPLE_STR = 64

_NULLISH = {"", "null", "na", "n/a", "nan", "none", "nil", "-"}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
_BOOLS = {"true", "false", "t", "f", "yes", "no", "y", "n", "0", "1"}
_DELIMS = [",", ";", "\t", "|"]


def _clean_name(raw: object, seen: dict[str, int]) -> str:
    name = str(raw or "").strip().lstrip("﻿").strip("\"'") or "column"
    name = re.sub(r"\s+", "_", name)
    if name in seen:
        seen[name] += 1
        name = f"{name}_{seen[name]}"
    else:
        seen[name] = 0
    return name[:128]


def _classify_value(value: Any) -> str | None:
    """Infer a coarse type from one value; None means null."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    text = str(value).strip()
    if text.lower() in _NULLISH:
        return None
    low = text.lower()
    if len(low) <= 5 and low in _BOOLS:
        return "boolean"
    if re.match(r"^[+-]?\d{1,18}$", text):
        return "integer"
    if re.match(r"^[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?$", text):
        return "number"
    if _TS_RE.match(text):
        return "timestamp"
    if _DATE_RE.match(text):
        return "date"
    return "string"


_TYPE_ORDER = {"boolean": 0, "integer": 1, "number": 2, "date": 3, "timestamp": 4, "string": 5}


def _merge_type(current: str | None, seen: str) -> str:
    """Widen a column's type as more values arrive (int + text -> string)."""
    if current is None:
        return seen
    if current == seen:
        return current
    return max(current, seen, key=lambda t: _TYPE_ORDER.get(t, 9))


class _ColumnAccumulator:
    __slots__ = ("name", "dtype", "nulls", "samples", "distinct", "rows")

    def __init__(self, name: str) -> None:
        self.name = name
        self.dtype: str | None = None
        self.nulls = 0
        self.samples: list[str] = []
        self.distinct: set[str] = set()
        self.rows = 0

    def add(self, value: Any) -> None:
        self.rows += 1
        kind = _classify_value(value)
        if kind is None:
            self.nulls += 1
            return
        self.dtype = _merge_type(self.dtype, kind)
        text = str(value)[:MAX_SAMPLE_STR]
        if len(self.samples) < MAX_SAMPLE_VALUES and text not in self.samples:
            self.samples.append(text)
        if len(self.distinct) < MAX_SAMPLE_ROWS:
            self.distinct.add(text)

    def finish(self) -> ColumnProfile:
        return ColumnProfile(
            name=self.name,
            data_type=self.dtype or "string",
            null_ratio=round(self.nulls / self.rows, 4) if self.rows else None,
            distinct_values=len(self.distinct),
            samples=self.samples,
        )


def infer_table_name(rel_path: str) -> str:
    """Table name a data file most likely becomes (its stem, sanitised)."""
    stem = rel_path.replace("\\", "/").rsplit("/", 1)[-1]
    stem = stem.rsplit(".", 1)[0] if "." in stem else stem
    return re.sub(r"[^0-9a-zA-Z]+", "_", stem).strip("_").lower() or "data"


def _build(
    *,
    path: str,
    fmt: str,
    size: int,
    headers: list[str],
    records: list[dict[str, Any]],
    row_count: int,
    truncated: bool,
    note: str | None = None,
) -> DataProfile:
    """Profile a list of already-decoded record dicts (JSON-flavoured inputs)."""
    seen: dict[str, int] = {}
    # one rename map for the whole file so a column keeps its name across rows
    rename: dict[str, str] = {}
    for record in records:
        for key in record:
            if key not in rename:
                rename[key] = _clean_name(key, seen)

    headers = [rename.get(h, h) for h in headers] if headers else list(rename.values())
    columns = [_ColumnAccumulator(h) for h in headers[:MAX_COLUMNS]]
    for record in records:
        for col in columns:
            col.add(record.get(col.name))

    return DataProfile(
        path=path,
        table=infer_table_name(path),
        format=fmt,
        row_count=row_count,
        sampled_rows=len(records),
        size_bytes=size,
        columns=[c.finish() for c in columns],
        truncated=truncated,
        note=note,
    )


def _sniff_delimiter(header_line: str) -> str:
    best, best_count = ",", 0
    for delim in _DELIMS:
        count = header_line.count(delim)
        if count > best_count:
            best, best_count = delim, count
    return best


def _profile_delimited(text: str, rel: str, size: int, fmt: str) -> DataProfile | None:
    first_line = text.split("\n", 1)[0]
    if not first_line.strip():
        return None
    reader = csv.reader(io.StringIO(text), delimiter=_sniff_delimiter(first_line))
    try:
        headers = next(reader)
    except (StopIteration, csv.Error):
        return None
    if not headers:
        return None

    seen: dict[str, int] = {}
    columns = [_ColumnAccumulator(_clean_name(h, seen)) for h in headers[:MAX_COLUMNS]]
    total = 0
    truncated = False
    malformed = False
    try:
        for row in reader:
            total += 1
            for col, value in zip(columns, row, strict=False):
                if col.rows < MAX_SAMPLE_ROWS:
                    col.add(value)
            if total > MAX_SAMPLE_ROWS:
                truncated = True
    except csv.Error as exc:
        # a malformed row (over-long field, ragged quoting) ends the scan; the
        # rows we did read are still the best evidence available
        log.debug("delimited scan stopped", file=rel, error=str(exc))
        malformed = True

    return DataProfile(
        path=rel,
        table=infer_table_name(rel),
        format=fmt,
        row_count=total,
        sampled_rows=min(total, MAX_SAMPLE_ROWS),
        size_bytes=size,
        columns=[c.finish() for c in columns],
        truncated=truncated or malformed,
        note="malformed rows ended the scan early" if malformed else None,
    )


def _json_records(doc: Any) -> list[Any] | None:
    if isinstance(doc, list):
        return doc
    if isinstance(doc, dict):
        for key in ("data", "rows", "records", "results", "items"):
            value = doc.get(key)
            if isinstance(value, list):
                return value
    return None


def _profile_json(text: str, rel: str, size: int, fmt: str) -> DataProfile | None:
    try:
        doc = json.loads(text)
    except (json.JSONDecodeError, RecursionError, ValueError):
        return None
    records = _json_records(doc)
    if not records:
        return None
    dicts = [r for r in records[:MAX_SAMPLE_ROWS] if isinstance(r, dict)]
    if not dicts:
        return None

    # column order: first appearance across the sampled records
    headers: list[str] = []
    for record in dicts:
        for key in record:
            if key not in headers:
                headers.append(key)

    total = len(records)
    return _build(
        path=rel,
        fmt=fmt,
        size=size,
        headers=headers,
        records=dicts,
        row_count=total,
        truncated=total > len(dicts),
        note=f"scanned the first {MAX_SCAN_BYTES:,} bytes only" if size > MAX_SCAN_BYTES else None,
    )


def _profile_jsonl(text: str, rel: str, size: int) -> DataProfile | None:
    records: list[dict[str, Any]] = []
    total = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        total += 1
        if len(records) < MAX_SAMPLE_ROWS:
            with contextlib.suppress(json.JSONDecodeError, ValueError):
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    records.append(parsed)
    if not records:
        return None

    headers: list[str] = []
    for record in records:
        for key in record:
            if key not in headers:
                headers.append(key)

    return _build(
        path=rel,
        fmt="jsonl",
        size=size,
        headers=headers,
        records=records,
        row_count=total,
        truncated=total > len(records),
        note=f"scanned the first {MAX_SCAN_BYTES:,} bytes only" if size > MAX_SCAN_BYTES else None,
    )


def _profile_parquet(raw: bytes, rel: str) -> DataProfile:
    """duckdb reads parquet from disk, so spool the upload to a temp file."""
    fallback = DataProfile(
        path=rel, table=infer_table_name(rel), format="parquet", size_bytes=len(raw)
    )
    try:
        import duckdb  # noqa: PLC0415 - optional dependency, cheap when unused
    except ImportError:
        fallback.note = "parquet profiling needs the duckdb package; columns unknown"
        return fallback

    described: list[tuple] = []
    row_count = 0
    rows: list[tuple] = []
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as handle:
            handle.write(raw)
            tmp = Path(handle.name)
        con = duckdb.connect()
        try:
            con.execute(f"CREATE TABLE _p AS SELECT * FROM read_parquet('{_sql(tmp)}')")
            described = con.execute("DESCRIBE _p").fetchall()
            counted = con.execute("SELECT count(*) FROM _p").fetchone()
            row_count = int(counted[0]) if counted else 0
            if row_count:
                rows = con.execute(f"SELECT * FROM _p LIMIT {MAX_SAMPLE_ROWS}").fetchall()
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 - a bad parquet must not fail intake
        log.warning("parquet profile failed", file=rel, error=str(exc))
        fallback.note = f"could not read parquet ({type(exc).__name__})"
        return fallback
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)

    columns = [_ColumnAccumulator(str(row[0])) for row in described]
    for row in rows:
        for col, value in zip(columns, row, strict=False):
            col.add(value)

    fallback.row_count = row_count
    fallback.sampled_rows = len(rows)
    fallback.columns = [c.finish() for c in columns]
    fallback.truncated = row_count > len(rows)
    return fallback


def _sql(path: Path) -> str:
    return str(path).replace("'", "''")


def profile_file(rel_path: str, raw: bytes) -> DataProfile | None:
    """Profile one data file. Returns None when there is nothing tabular."""
    size = len(raw)
    name = rel_path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""

    if ext == "parquet":
        return _profile_parquet(raw, rel_path)

    if ext in {"csv", "tsv"}:
        text = raw[:MAX_SCAN_BYTES].decode("utf-8", errors="replace")
        profile = _profile_delimited(text, rel_path, size, "tsv" if ext == "tsv" else "csv")
        if profile is not None and size > MAX_SCAN_BYTES:
            profile.truncated = True
            profile.note = f"scanned the first {MAX_SCAN_BYTES:,} bytes only"
        return profile

    if ext in {"jsonl", "ndjson"}:
        text = raw[:MAX_SCAN_BYTES].decode("utf-8", errors="replace")
        profile = _profile_jsonl(text, rel_path, size)
        if profile is not None and size > MAX_SCAN_BYTES:
            profile.truncated = True
            profile.note = f"scanned the first {MAX_SCAN_BYTES:,} bytes only"
        return profile

    if ext == "json":
        text = raw[:MAX_SCAN_BYTES].decode("utf-8", errors="replace")
        return _profile_json(text, rel_path, size, "json")

    return None


__all__ = [
    "MAX_SAMPLE_ROWS",
    "MAX_SCAN_BYTES",
    "infer_table_name",
    "profile_file",
]