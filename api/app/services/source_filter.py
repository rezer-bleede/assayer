"""One source of truth for what happens to each file the user hands us.

Shared by two intake paths that must not drift:

* ``repo_fetch.collect()`` — walking a git clone.
* ``routers/etl.py`` upload — files the user picked/dragged (often a whole
  project directory, so the junk is the norm, not the exception).

Every path is routed to one of four destinations *before* its bytes are used:

``CODE``    transformation logic and dbt/project config — read, parsed, lineage.
``DATA``    CSVs, JSON arrays, NDJSON, parquet — profiled into column metadata
            and a table name; the body never becomes source or context.
``SNIFF``   ambiguous by name alone (a generic ``.json``) — decided by content.
``SKIP``    nothing here: dependencies, VCS metadata, binaries, lock files,
            generated/minified artifacts, bulk data dumps.

Path/size rules run before anything is read; the content sniffs run only on
candidates that survived them. dbt's ``target/manifest.json`` is a named
exception: it is lineage, so it gets its own (larger) byte budget.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.schemas import DataProfile

# directories that are never ETL source and routinely dwarf everything else
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env",
    "__pycache__", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", ".idea", ".vscode", "site-packages", ".next", "logs",
    ".ipynb_checkpoints",
}

# transformation logic: parsed per file into lineage
CODE_EXT = {".sql", ".py", ".ddl", ".j2", ".jinja"}
# project config: parsed for dbt profiles, sources, connection strings
CONFIG_EXT = {".yml", ".yaml"}
# tabular data: profiled, never read as source
DATA_EXT = {".csv", ".tsv", ".jsonl", ".ndjson", ".parquet"}
# ...of which these are binary on disk and must not be sniffed for control chars
BINARY_DATA_EXT = {".parquet"}
# ambiguous by name: `.json` is dbt config as often as it is an export
JSON_EXT = {".json"}

# extensionless / unusual config files worth reading regardless of the above
CONFIG_NAMES = {
    "dbt_project.yml", "profiles.yml", "packages.yml", "selectors.yml",
    "dbt_packages.yml",
}

# lock files and build artifacts: large, mechanical, zero lineage value. The
# dbt `catalog.json` is on the list because it duplicates introspected schemas.
JUNK_NAMES = {
    "package-lock.json", "package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml",
    "yarn.lock", "bun.lock", "bun.lockb", "poetry.lock", "pipfile.lock",
    "uv.lock", "conda-lock.yml", "composer.lock", "gemfile.lock",
    "catalog.json", "run_results.json", "semgrep.lock", ".ds_store",
}

# dbt compiles models into target/; that SQL mirrors the models and would
# double-count lineage. Only the manifest from target/ is useful.
_TARGET_KEEP = {"manifest.json"}

# JSON documents shaped like these keys are configuration, not data exports
_JSON_CONFIG_KEYS = {
    "name", "version", "profile", "config", "models", "seeds", "sources",
    "vars", "data_tests", "tests", "on-run-start", "on-run-end",
    "on_run_start", "on_run_end", "type", "path", "path-paths",
}
# ...while these wrap a row payload, which makes the document a data export
_JSON_DATA_KEYS = {"data", "rows", "records", "results", "items", "values", "features"}

# a JSON config worth reading is small; anything bigger is a data export, a
# lock file or a generated artefact, and gets profiled instead of parsed
JSON_CONFIG_MAX_BYTES = 32_000

_BIN_SNIFF_BYTES = 4096
# NUL anywhere in the sniff window, or a lot of control chars, means binary
_CTRL_RE = re.compile(rb"[\x00-\x08\x0e-\x1f]")

# content sniffs for text we were about to treat as code/config
_MINIFY_LONG_LINE = 500      # chars
_MINIFY_RATIO = 0.4          # share of lines longer than the above
_DUMP_RUN_LINES = 300        # consecutive bulk-data lines
_BULK_LINE_RE = re.compile(r"^\s*(insert\s+into|values\s*\(|\(|--\s+insert)", re.I)
_LONG_INT_RE = re.compile(r"^[+-]?\d{1,18}$")
_FLOAT_RE = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?$")


class Kind(StrEnum):
    CODE = "code"
    CONFIG = "config"
    DATA = "data"
    SNIFF = "sniff"   # generic .json: needs content to decide
    SKIP = "skip"


class Skip(StrEnum):
    KEEP = "keep"
    DIR = "skip_dir"              # lives under .git / node_modules / …
    TYPE = "skip_type"            # extension we never read
    JUNK = "skip_junk"            # lock file / package manifest / build artifact
    SIZE = "skip_size"            # above the per-file cap
    TOO_MANY = "skip_too_many"
    TARGET_NOISE = "skip_compiled"  # dbt target/ artifacts other than manifest
    BINARY = "skip_binary"
    GENERATED = "skip_generated"  # minified / machine-written
    DATA_DUMP = "skip_data_dump"  # bulk INSERT statements posing as SQL
    TOTAL = "skip_total_cap"      # over the aggregate source budget


@dataclass(frozen=True, slots=True)
class Verdict:
    kind: Kind
    reason: Skip = Skip.KEEP

    @property
    def keep(self) -> bool:
        return self.kind is not Kind.SKIP


KEEP_CODE = Verdict(Kind.CODE)
KEEP_CONFIG = Verdict(Kind.CONFIG)
KEEP_DATA = Verdict(Kind.DATA)
KEEP_SNIFF = Verdict(Kind.SNIFF)


def _parts(rel_path: str) -> list[str]:
    norm = rel_path.replace("\\", "/")
    # strip leading "/" and "./" *segments* — never lstrip("./"), which would
    # eat the dot in ".git" and let VCS metadata through.
    return norm.removeprefix("/").removeprefix("./").removeprefix("/").split("/")


def is_seed_csv(rel_path: str) -> bool:
    """A dbt seed: the *name* says "this becomes a table"; the bytes do not.

    Anchored on a real `seeds/` path segment (or an exact `seed_<name>.csv`),
    so `research/seedlings.csv` is data, not a seed.
    """
    parts = _parts(rel_path.lower())
    if posixpath.splitext(parts[-1])[1] != ".csv":
        return False
    return "seeds" in parts[:-1] or parts[-1].startswith("seed_")


def is_data_ext(rel_path: str) -> bool:
    return posixpath.splitext(_parts(rel_path)[-1])[1].lower() in DATA_EXT


def is_binary_format(rel_path: str) -> bool:
    """Data formats that are legitimately binary (parquet is not a text file)."""
    return posixpath.splitext(_parts(rel_path)[-1])[1].lower() in BINARY_DATA_EXT


def seed_table_name(rel_path: str) -> str:
    """Table name a dbt seed will be materialised as (its file stem)."""
    return posixpath.splitext(_parts(rel_path)[-1])[0]


def classify(
    rel_path: str,
    *,
    size: int | None = None,
    max_file_bytes: int | None = None,
    manifest_max_bytes: int | None = None,
    data_max_bytes: int | None = None,
    trust: bool = False,
) -> Verdict:
    """Where does this path go? ``trust`` means "the user picked this by name".

    Path/size rules only — no content is read here. Ambiguous `.json` comes back
    as `SNIFF` for the caller to resolve with :func:`sniff_text`.
    """
    parts = _parts(rel_path)
    name = parts[-1]
    low = name.lower()

    if any(p in SKIP_DIRS for p in parts[:-1]) or parts[0] in SKIP_DIRS:
        return Verdict(Kind.SKIP, Skip.DIR)

    # dbt's target/ can sit anywhere below the repo root (a folder upload is
    # prefixed by the project dir); only .../target/manifest.json counts.
    target_dirs = [i for i, p in enumerate(parts[:-1]) if p == "target"]
    if target_dirs:
        under = parts[target_dirs[0] + 1 :]
        if not (len(under) == 1 and under[0] in _TARGET_KEEP):
            return Verdict(Kind.SKIP, Skip.TARGET_NOISE)

    ext = posixpath.splitext(low)[1]
    is_manifest = low in _TARGET_KEEP and bool(target_dirs)

    # A deliberate pick is a deliberate pick: no junk list, no extension
    # filtering, no data-vs-config guessing. Only the unreadable stays unread.
    if trust:
        kind = Kind.CONFIG if ext in CONFIG_EXT else Kind.CODE
    elif low in JUNK_NAMES:
        return Verdict(Kind.SKIP, Skip.JUNK)
    elif ext in CODE_EXT:
        kind = Kind.CODE
    elif ext in CONFIG_EXT or low in CONFIG_NAMES:
        kind = Kind.CONFIG
    elif ext in DATA_EXT:
        kind = Kind.DATA
    elif ext in JSON_EXT:
        kind = Kind.SNIFF if not is_manifest else Kind.CONFIG
    elif low in _TARGET_KEEP:
        kind = Kind.CONFIG
    else:
        return Verdict(Kind.SKIP, Skip.TYPE)

    limit = (
        manifest_max_bytes
        if is_manifest and manifest_max_bytes is not None
        else data_max_bytes
        if kind is Kind.DATA and data_max_bytes is not None
        else max_file_bytes
    )
    if size is not None and limit is not None and size > limit:
        return Verdict(Kind.SKIP, Skip.SIZE)
    return Verdict(kind)


def looks_binary(raw: bytes) -> bool:
    if b"\x00" in raw[:_BIN_SNIFF_BYTES]:
        return True
    window = raw[:_BIN_SNIFF_BYTES]
    if not window:
        return False
    # control chars are legal in source text only rarely; a high ratio is a
    # compiled asset, parquet, image, zip … wearing a .txt/.json suffix.
    return len(_CTRL_RE.findall(window)) / len(window) > 0.30


def looks_generated(text: str) -> bool:
    """Minified bundles and machine-written blobs carry no readable lineage."""
    lines = text.splitlines()
    if not lines:
        return False
    if len(lines) == 1:
        return len(lines[0]) > 8_000
    long_lines = sum(1 for line in lines if len(line) > _MINIFY_LONG_LINE)
    return long_lines / len(lines) > _MINIFY_RATIO


def looks_like_dump(text: str) -> bool:
    """A `.sql` file that is really rows of data (bulk INSERT / VALUES)."""
    run = 0
    for line in text.splitlines():
        if _BULK_LINE_RE.match(line):
            run += 1
            if run >= _DUMP_RUN_LINES:
                return True
        elif line.strip():
            run = 0
    return False


def sniff_json(text: str) -> Kind:
    """Config or data export? A JSON array is always data."""
    stripped = text.lstrip()[:1]
    if stripped == "[":
        return Kind.DATA
    if stripped != "{":
        return Kind.SKIP
    try:
        doc = json.loads(text)
    except (json.JSONDecodeError, RecursionError, ValueError):
        return Kind.SKIP
    if not isinstance(doc, dict):
        return Kind.DATA

    values = list(doc.values())
    keys = {str(k).lower() for k in doc}
    # a list of objects under any key is a row payload, not configuration
    if any(isinstance(v, list) and v and isinstance(v[0], dict) for v in values):
        return Kind.DATA
    if keys & _JSON_DATA_KEYS and any(isinstance(v, list) for v in values):
        return Kind.DATA
    if keys & _JSON_CONFIG_KEYS:
        return Kind.CONFIG
    if len(doc) > 30:
        return Kind.SKIP
    if any(isinstance(v, list) for v in values):
        return Kind.DATA
    # small objects with unknown keys: harmless to read, occasionally useful
    # (tsconfig-shaped, editor settings, a hand-rolled connection block)
    return Kind.CONFIG


def sniff_text(rel_path: str, text: str) -> Verdict:
    """Second-stage routing for content-dependent candidates."""
    ext = posixpath.splitext(_parts(rel_path)[-1].lower())[1]
    if ext in JSON_EXT:
        kind = sniff_json(text)
        return Verdict(kind) if kind is not Kind.SKIP else Verdict(Kind.SKIP, Skip.TYPE)
    if looks_generated(text):
        return Verdict(Kind.SKIP, Skip.GENERATED)
    if ext in CODE_EXT and looks_like_dump(text):
        return Verdict(Kind.SKIP, Skip.DATA_DUMP)
    return KEEP_CODE if ext in CODE_EXT else KEEP_CONFIG


@dataclass(slots=True)
class Intake:
    """The outcome of routing a pile of files."""

    contents: dict[str, str] = field(default_factory=dict)   # code/config, parsed
    manifest: dict[str, Any] | None = None                    # dbt manifest
    profiles: list[DataProfile] = field(default_factory=list)  # profiled data files
    notes: list[str] = field(default_factory=list)            # what we ignored, and why

    @property
    def blob(self) -> str:
        return "\n".join(self.contents.values())


def assemble_sources(
    items: Iterable[tuple[str, bytes]],
    *,
    max_files: int,
    max_file_bytes: int,
    manifest_max_bytes: int = 4_000_000,
    data_max_bytes: int | None = None,
    max_total_bytes: int | None = None,
    max_data_files: int | None = None,
    trust: bool = False,
) -> Intake:
    """Turn raw ``(rel_path, bytes)`` pairs into code, profiles and notes.

    Anything irrelevant is skipped rather than fatal — a project folder always
    contains noise. Data files are the deliberate exception: they are profiled
    (columns, types, row count) and their *name* is registered, but their body
    never enters the source text or the analysis context.
    """
    from app.services.profile import profile_file  # local: profile is stdlib-only

    out = Intake()
    dropped: dict[str, int] = {}
    total = 0

    def note(reason: str) -> None:
        dropped[reason] = dropped.get(reason, 0) + 1

    for rel_path, raw in items:
        if len(out.contents) >= max_files:
            note(str(Skip.TOO_MANY))
            continue
        verdict = classify(
            rel_path,
            size=len(raw),
            max_file_bytes=max_file_bytes,
            manifest_max_bytes=manifest_max_bytes,
            data_max_bytes=data_max_bytes,
            trust=trust,
        )
        name = _parts(rel_path)[-1]

        if verdict.kind is Kind.SKIP:
            note(str(verdict.reason))
            continue

        # seeds: the name says "this becomes a table". Keep it visible to the
        # parsers (empty body) and profile the columns; never read it as code.
        if is_seed_csv(rel_path):
            out.contents.setdefault(rel_path, "")

        data_route = verdict.kind is Kind.DATA or verdict.kind is Kind.SNIFF
        if data_route:
            # parquet *is* binary; sniffing it for control chars would throw
            # away the most valuable data file a warehouse project ships
            if not is_binary_format(rel_path) and looks_binary(raw):
                note(str(Skip.BINARY))
                continue
            if verdict.kind is Kind.SNIFF:
                # a generic .json is source only if it is small and really
                # looks like configuration; otherwise it is a data export
                body = raw.decode("utf-8", errors="replace")
                decision = (
                    Kind.DATA
                    if len(body) > JSON_CONFIG_MAX_BYTES
                    else sniff_json(body)
                )
                if decision is Kind.SKIP:
                    note(str(Skip.TYPE))
                    continue
                if decision is Kind.CONFIG:
                    verdict = Verdict(Kind.CONFIG)
                    data_route = False
            if data_route:
                if max_data_files is not None and len(out.profiles) >= max_data_files:
                    note("over-data-file-cap")
                    continue
                profile = profile_file(rel_path, raw)
                if profile is not None:
                    out.profiles.append(profile)
                else:
                    note("unprofilable")
                continue

        if out.manifest is None and name in _TARGET_KEEP:
            parsed = _try_manifest(raw)
            if parsed is not None:
                out.manifest = parsed
                continue

        if looks_binary(raw):
            note(str(Skip.BINARY))
            continue
        text = raw.decode("utf-8", errors="replace")
        if text.count("\ufffd") > max(20, len(text) // 20):
            note(str(Skip.BINARY))
            continue
        if not trust:
            resolved = sniff_text(rel_path, text)
            if resolved.kind is Kind.SKIP:
                note(str(resolved.reason))
                continue

        if max_total_bytes is not None and total + len(text) > max_total_bytes:
            note(str(Skip.TOTAL))
            continue
        total += len(text)
        out.contents[rel_path] = text

    out.notes = _summarise(dropped)
    return out


def _try_manifest(raw: bytes) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) and "nodes" in parsed else None


def _summarise(dropped: dict[str, int]) -> list[str]:
    human = {
        str(Skip.DIR): "dependency / VCS metadata directories (.git, node_modules, …)",
        str(Skip.TYPE): "files we do not parse",
        str(Skip.JUNK): "lock files and package manifests",
        str(Skip.SIZE): "files above the size cap",
        str(Skip.TARGET_NOISE): "dbt target/ build artifacts (compiled SQL etc.)",
        str(Skip.TOO_MANY): "files beyond the count cap",
        str(Skip.BINARY): "binary files with source-looking names",
        str(Skip.GENERATED): "generated or minified files",
        str(Skip.DATA_DUMP): "bulk data files stored as SQL",
        str(Skip.TOTAL): "files beyond the total source budget",
        "over-data-file-cap": "data files beyond the profiling cap",
        "unprofilable": "data files with no readable rows",
        "over-file-cap": "files beyond the count cap",
    }
    if not dropped:
        return []
    parts = [f"{human.get(k, k)}: {v:,}" for k, v in sorted(dropped.items())]
    total = sum(dropped.values())
    return [
        f"Ignored {total:,} irrelevant file(s) from the upload ({'; '.join(parts)}). "
        "Only ETL-relevant sources were analysed."
    ]


def profile_summary(profiles: Sequence[DataProfile], limit: int = 4) -> str:
    """One line describing the data files we profiled, for chat and notes."""
    if not profiles:
        return ""
    shown = ", ".join(p.describe() for p in profiles[:limit])
    more = f" (+{len(profiles) - limit} more)" if len(profiles) > limit else ""
    return f"Profiled {len(profiles)} data file(s): {shown}{more}."


__all__ = [
    "DATA_EXT",
    "CODE_EXT",
    "CONFIG_EXT",
    "Intake",
    "Kind",
    "Skip",
    "Verdict",
    "assemble_sources",
    "classify",
    "is_binary_format",
    "is_data_ext",
    "is_seed_csv",
    "looks_binary",
    "looks_generated",
    "looks_like_dump",
    "profile_summary",
    "seed_table_name",
    "sniff_json",
    "sniff_text",
]