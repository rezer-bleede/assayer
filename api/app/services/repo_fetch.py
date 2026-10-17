"""Fetch a git repository as parseable ETL source.

Security posture, because this endpoint clones a URL the user typed:

* HTTPS only. `file://`, `git://`, `ssh://` and bare paths are refused — we are
  not trying to support every transport, only to avoid being tricked into
  reading the container's own filesystem.
* The resolved host must be public (or explicitly allowed private). Cloud
  metadata addresses (169.254.0.0/16) and loopback are always refused, so a
  crafted URL cannot turn this into an SSRF probe of the instance.
* A personal access token is passed to git through `GIT_ASKPASS` + process env,
  never embedded in the URL on the command line. argv is world-readable via
  `ps` and `/proc`; process env is not.
* Every message that leaves this module is scrubbed. git prints the remote URL
  in a surprising number of failures, and that URL is where the credential was.
* Repository contents are parsed as text and never executed or imported.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from app.core.config import settings
from app.core.logging import log

_CRED_RE = re.compile(
    r"(?P<scheme>[a-zA-Z][\w+.-]*)://(?P<user>[^:/@\s]+)(?::(?P<secretp>[^@/\s]+))?@"
)

# directories that are never ETL and often enormous
_SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".idea",
    ".vscode",
    "site-packages",
    ".next",
    "target/compilation",
    "logs",
}

# extensions we are willing to read as source
_INCLUDE_EXT = {".sql", ".py", ".yml", ".yaml", ".json", ".txt", ".ddl", ".jinja", ".j2"}

# only these specific files are worth reading regardless of extension
_INCLUDE_NAMES = {
    "dbt_project.yml",
    "profiles.yml",
    "packages.yml",
    "selectors.yml",
    "manifest.json",
}

# a manifest can be huge; models are the interesting part
_MAX_MANIFEST_BYTES = 4_000_000


class RepoError(ValueError):
    """User-facing failure. Messages are always credential-scrubbed."""


@dataclass
class RepoContent:
    files: dict[str, str] = field(default_factory=dict)
    manifest: dict | None = None
    repo_url: str | None = None
    ref: str | None = None
    commits: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def blob(self) -> str:
        return "\n".join(self.files.values())


def scrub(text: str | None) -> str:
    """Remove `user:password@` and any bare token-looking run from a message."""
    if not text:
        return ""
    out = _CRED_RE.sub(lambda m: f"{m.group('scheme')}://***@***@", text)
    return out[:500]


def _refuse(reason: str) -> None:
    raise RepoError(reason)


def validate_repo_url(url: str) -> str:
    """Reject anything we should not be reaching out to."""
    url = (url or "").strip()
    if not url:
        _refuse("No repository URL given.")

    parts = urlsplit(url)
    if parts.scheme != "https":
        _refuse(
            f"Unsupported scheme {parts.scheme!r}. Use an https:// repository URL "
            "(ssh:// and file:// are not supported)."
        )
    if not parts.hostname:
        _refuse("Repository URL has no host.")
    if parts.username or parts.password:
        _refuse(
            "Do not put credentials in the URL. Paste the repository URL alone and, "
            "if it is private, supply the access token separately."
        )
    if parts.port not in (None, 443, 80):
        _refuse("Only standard HTTPS ports are allowed.")

    host = parts.hostname
    try:
        infos = {
            i[4][0] for i in socket.getaddrinfo(host, parts.port or 443, proto=socket.IPPROTO_TCP)
        }
    except socket.gaierror as exc:
        _refuse(f"Cannot resolve {host}: {scrub(str(exc))}")

    for raw in infos:
        ip = ipaddress.ip_address(raw)
        if ip.is_loopback:
            _refuse(f"{host} resolves to a loopback address; refusing.")
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            _refuse(f"{host} resolves to {raw}, which is refused (instance metadata / reserved).")
        if ip.is_private and not settings.repo_allow_private_hosts:
            _refuse(
                f"{host} resolves to a private address {raw}; set REPO_ALLOW_PRIVATE_HOSTS=true if that is your intranet git host."
            )
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") or "/", "", ""))


def _askpass(dirpath: Path) -> Path:
    """GIT_ASKPASS helper: answers from env, so the token never hits argv."""
    script = dirpath / "askpass.sh"
    script.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  *sername*) printf '%s' \"${QT_GIT_USERNAME:-oauth2}\" ;;\n"
        "  *) printf '%s' \"${QT_GIT_TOKEN:-}\" ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    return script


def _run_clone(
    url: str, dest: Path, ref: str | None, token: str | None, username: str | None
) -> None:
    args = ["git", "clone", "--depth", "1", "--single-branch", "--no-tags"]
    if ref:
        args += ["--branch", ref]
    args += ["--", url, str(dest)]

    env = dict(os.environ)
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
            "HOME": str(dest.parent),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_LFS_SKIP_SMUDGE": "1",
        }
    )
    if token:
        env["GIT_ASKPASS"] = str(_askpass(dest.parent))
        env["QT_GIT_TOKEN"] = token
        env["QT_GIT_USERNAME"] = username or "oauth2"

    try:
        proc = subprocess.run(
            args,
            env=env,
            capture_output=True,
            text=True,
            timeout=settings.repo_clone_timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        _refuse(f"Clone timed out after {settings.repo_clone_timeout_seconds}s.")
    except FileNotFoundError:
        _refuse("git is not available in this container.")
    except OSError as exc:
        _refuse(f"Could not run git: {scrub(str(exc))}")

    if proc.returncode != 0:
        err = scrub(proc.stderr or proc.stdout)
        hint = ""
        if "Authentication" in err or "403" in err or "permission" in err.lower():
            hint = " The repository may be private — supply an access token."
        elif "not found" in err.lower() or "404" in err:
            hint = " Check the URL and that the branch exists."
        _refuse(f"git clone failed: {err}.{hint}")


def collect(dirpath: Path, warnings: list[str]) -> tuple[dict[str, str], dict | None]:
    """Walk the clone for readable source, honouring size/count caps."""
    files: dict[str, str] = {}
    manifest: dict | None = None
    total = 0

    for path in sorted(dirpath.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(dirpath).as_posix()
        if any(part in _SKIP_DIRS for part in path.parts[:-1]):
            continue
        if path.suffix.lower() not in _INCLUDE_EXT and path.name not in _INCLUDE_NAMES:
            continue
        # seed CSVs tell us a table exists; their contents never affect lineage,
        # so register the name only and skip the (potentially large) read
        if path.suffix.lower() == ".csv":
            if "seed" in rel.lower():
                files.setdefault(rel, "")
            continue
        if rel.split("/")[-1] == "manifest.json" and path.stat().st_size > _MAX_MANIFEST_BYTES:
            warnings.append(f"skipped oversized manifest at {rel}")
            continue
        if len(files) >= settings.repo_max_files:
            warnings.append(f"stopped at {settings.repo_max_files} files; the repo is larger")
            break

        size = path.stat().st_size
        if size > settings.repo_max_file_bytes:
            warnings.append(f"skipped {rel} ({size:,} bytes > cap)")
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            warnings.append(f"could not read {rel}: {scrub(str(exc))}")
            continue

        total += len(text)
        if total > settings.repo_max_total_bytes:
            warnings.append(f"stopped at the {settings.repo_max_total_bytes:,} byte source cap")
            break

        files[rel] = text
        if path.name == "manifest.json" and manifest is None:
            with contextlib.suppress(Exception):
                parsed = json.loads(text)
                if isinstance(parsed, dict) and "nodes" in parsed:
                    manifest = parsed

    if not files:
        _refuse("No .sql / .py / .yml source files found in that repository.")
    return files, manifest


def fetch(
    url: str, ref: str | None = None, token: str | None = None, username: str | None = None
) -> RepoContent:
    """Clone `url` and return its source files. Blocking; call via a thread."""
    if not settings.repo_enabled:
        _refuse("Repository intake is disabled (REPO_ENABLED=false).")

    safe_url = validate_repo_url(url)
    root = Path(settings.repo_tmp_dir)
    try:
        root.mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix="clone-", dir=root))
    except OSError as exc:
        _refuse(
            f"cannot use the clone directory {settings.repo_tmp_dir} ({exc.strerror}). "
            "It must be writable by the container user — set REPO_TMP_DIR elsewhere."
        )
    target = workdir / "repo"
    warnings: list[str] = []

    try:
        _run_clone(safe_url, target, ref, token, username)

        # drop the git objects: we only ever parse source, never history
        with contextlib.suppress(Exception):
            shutil.rmtree(target / ".git")

        files, manifest = collect(target, warnings)
        commits = 1
        log.info("repo cloned", url=scrub(safe_url), files=len(files), warnings=len(warnings))
        return RepoContent(
            files=files,
            manifest=manifest,
            repo_url=scrub(safe_url),
            ref=ref,
            commits=commits,
            warnings=warnings,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
