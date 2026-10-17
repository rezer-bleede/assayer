"""One source of truth for "is this file a relevant piece of ETL source?".

Shared by two intake paths that must not drift:

* ``repo_fetch.collect()`` — walking a git clone.
* ``routers/etl.py`` upload — files the user picked/dragged (often a whole
  project directory, so the junk is the norm, not the exception).

Classification is path/size/binary-shape based so it runs before anything is
read into memory or decoded. Seed CSVs and dbt's ``target/manifest.json`` are
special cases that need a rule of their own rather than a broader extension
list, because reading their bytes is useless or expensive while the *names*
carry real information (seeds are physical tables; the manifest is lineage).
"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Iterable
from enum import StrEnum
from typing import Any

# directories that are never ETL source and routinely dwarf everything else
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env",
    "__pycache__", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", ".idea", ".vscode", "site-packages", ".next", "logs",
    ".ipynb_checkpoints",
}

# extensions we are willing to read as text
INCLUDE_EXT = {".sql", ".py", ".yml", ".yaml", ".json", ".txt", ".ddl", ".j2", ".jinja"}

# extensionless / unusual files worth reading regardless of the above
INCLUDE_NAMES = {"dbt_project.yml", "profiles.yml", "packages.yml", "selectors.yml", "manifest.json"}

# dbt compiles models into target/; that SQL mirrors the models and would
# double-count lineage. Only the manifest from target/ is useful.
_TARGET_KEEP = {"manifest.json"}

_BIN_SNIFF_BYTES = 4096
# NUL anywhere in the sniff window, or a lot of control chars, means binary
_CTRL_RE = re.compile(rb"[\x00-\x08\x0e-\x1f]")


class Skip(StrEnum):
    KEEP = "keep"
    DIR = "skip_dir"          # lives under .git / node_modules / …
    TYPE = "skip_type"        # extension we never read
    SIZE = "skip_size"        # above the per-file cap
    TOO_MANY = "skip_too_many"
    TARGET_NOISE = "skip_compiled"  # dbt target/ artifacts other than manifest
    BINARY = "skip_binary"


def classify(
    rel_path: str,
    *,
    size: int | None = None,
    max_file_bytes: int | None = None,
    manifest_max_bytes: int | None = None,
) -> Skip:
    """Should this path be read? ``KEEP`` yes, otherwise the reason."""
    norm = rel_path.replace("\\", "/")
    # strip leading "/" and "./" *segments* — never lstrip("./"), which would
    # eat the dot in ".git" and let VCS metadata through.
    norm = norm.removeprefix("/").removeprefix("./").removeprefix("/")
    parts = norm.split("/")
    name = parts[-1]

    if any(p in SKIP_DIRS for p in parts[:-1]) or parts[0] in SKIP_DIRS:
        return Skip.DIR

    # dbt's target/ can sit anywhere below the repo root (a folder upload is
    # prefixed by the project dir); only .../target/manifest.json counts.
    target_dirs = [i for i, p in enumerate(parts[:-1]) if p == "target"]
    if target_dirs:
        under = parts[target_dirs[0] + 1 :]
        if not (len(under) == 1 and under[0] in _TARGET_KEEP):
            return Skip.TARGET_NOISE

    ext = posixpath.splitext(name)[1].lower()
    if ext not in INCLUDE_EXT and name not in INCLUDE_NAMES:
        return Skip.TYPE

    if size is not None and max_file_bytes is not None:
        is_manifest = name in _TARGET_KEEP and bool(target_dirs)
        limit = (
            manifest_max_bytes
            if is_manifest and manifest_max_bytes is not None
            else max_file_bytes
        )
        if size > limit:
            return Skip.SIZE

    return Skip.KEEP


def is_seed_csv(rel_path: str) -> bool:
    """A dbt seed: the *name* says "this becomes a table"; the bytes do not."""
    norm = rel_path.replace("\\", "/").lower()
    ext = posixpath.splitext(norm)[1]
    return ext == ".csv" and ("seed" in norm or "/seeds/" in norm or norm.startswith("seeds/"))


def looks_binary(raw: bytes) -> bool:
    if b"\x00" in raw[:_BIN_SNIFF_BYTES]:
        return True
    window = raw[:_BIN_SNIFF_BYTES]
    if not window:
        return False
    # control chars are legal in source text only rarely; a high ratio is a
    # compiled asset, parquet, image, zip … wearing a .txt/.json suffix.
    return len(_CTRL_RE.findall(window)) / len(window) > 0.30


def assemble_sources(
    items: Iterable[tuple[str, bytes]],
    *,
    max_files: int,
    max_file_bytes: int,
    manifest_max_bytes: int = 4_000_000,
) -> tuple[dict[str, str], dict[str, Any] | None, list[str]]:
    """Turn raw ``(rel_path, bytes)`` pairs into clean ``(contents, manifest, notes)``.

    Anything irrelevant is skipped rather than fatal — a project folder always
    contains noise. The notes summarise what was dropped so the agent can tell
    the user, instead of silently under-reporting the codebase.
    """
    contents: dict[str, str] = {}
    manifest: dict[str, Any] | None = None
    dropped: dict[str, int] = {}

    def note(reason: str) -> None:
        dropped[reason] = dropped.get(reason, 0) + 1

    for rel_path, raw in items:
        if len(contents) >= max_files:
            note("over-file-cap")
            continue
        verdict = classify(
            rel_path,
            size=len(raw),
            max_file_bytes=max_file_bytes,
            manifest_max_bytes=manifest_max_bytes,
        )
        name = rel_path.replace("\\", "/").rsplit("/", 1)[-1]

        # seeds: register the table, skip reading the (often large) CSV body
        if is_seed_csv(rel_path):
            contents.setdefault(rel_path, "")
            continue

        if verdict is Skip.KEEP:
            if name == "manifest.json" and manifest is None:
                try:
                    parsed = json.loads(raw.decode("utf-8", errors="replace"))
                    if isinstance(parsed, dict) and "nodes" in parsed:
                        manifest = parsed
                        continue
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass  # not a dbt manifest; fall through to normal handling
            if looks_binary(raw):
                note("binary")
                continue
            text = raw.decode("utf-8", errors="replace")
            if text.count("\ufffd") > max(20, len(text) // 20):
                note("binary")
                continue
            contents[rel_path] = text
        else:
            note(str(verdict))

    notes = _summarise(dropped)
    return contents, manifest, notes


def _summarise(dropped: dict[str, int]) -> list[str]:
    human = {
        str(Skip.DIR): "dependency / VCS metadata directories (.git, node_modules, …)",
        str(Skip.TYPE): "files we do not parse",
        str(Skip.SIZE): "files above the size cap",
        str(Skip.TARGET_NOISE): "dbt target/ build artifacts (compiled SQL etc.)",
        str(Skip.TOO_MANY): "files beyond the count cap",
        "binary": "binary files with source-looking names",
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
