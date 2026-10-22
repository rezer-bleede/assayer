"""Ingestion of connection info and ETL code supplied via chat text.

This is what lets the user "just paste everything in the chat window".
"""

from __future__ import annotations

import json
import logging
import re

from app.schemas import ConnectionSpec, EtlLanguage, SourceType

log = logging.getLogger(__name__)

# NOTE: The `db` group is greedy ([^?\s]*) so it captures the full path
# component.  Prose that immediately follows the database name (e.g.
# "property_intelI expect: ...") is cleaned up afterwards in parse_url via
# _clean_db(), which stops at the first camelCase transition or sentence
# punctuation that signals natural language rather than a real identifier.
_URL_RE = re.compile(
    r"(?P<scheme>[a-z0-9+]+)://"
    r"(?:(?P<user>[^:/@\s]+)(?::(?P<pw>[^@\s]+))?@)?"
    r"(?P<host>[^:/?@\s]+)(?::(?P<port>\d+))?"
    r"(?:/(?P<db>[^?\s]*))?"
    r"(?:\?(?P<params>\S+))?",
    re.I,
)

# Strips trailing prose from a database/path segment captured by _URL_RE.
# Matches the first camelCase transition (lowercase/digit immediately before
# an uppercase letter), a sentence-boundary punctuation, or whitespace.
_DB_PROSE_RE = re.compile(r"(?<=[a-z0-9_])(?=[A-Z])|[.!?,;:]+.*$")


def _clean_db(raw: str | None) -> str | None:
    """Trim trailing natural-language prose from a captured database name."""
    if not raw:
        return raw or None
    cleaned = _DB_PROSE_RE.split(raw, maxsplit=1)[0]
    return cleaned or None


_SCHEME_MAP = {
    "postgresql": SourceType.POSTGRES,
    "postgres": SourceType.POSTGRES,
    "mysql": SourceType.MYSQL,
    "mariadb": SourceType.MYSQL,
    "snowflake": SourceType.SNOWFLAKE,
    "mssql": SourceType.MSSQL,
    "sqlserver": SourceType.MSSQL,
    "sqlite": SourceType.SQLITE,
    "bigquery": SourceType.BIGQUERY,
    "databricks": SourceType.DATABRICKS,
    "s3": SourceType.S3_CSV,
    # clickhouse-connect speaks HTTP; the native 9000 TCP protocol is not it
    "clickhouse": SourceType.CLICKHOUSE,
    "clickhouse+http": SourceType.CLICKHOUSE,
    "clickhouse+https": SourceType.CLICKHOUSE,
    "ch": SourceType.CLICKHOUSE,
}

_WORDS = {
    "clickhouse": SourceType.CLICKHOUSE,
    "postgres": SourceType.POSTGRES,
    "postgresql": SourceType.POSTGRES,
    "mysql": SourceType.MYSQL,
    "snowflake": SourceType.SNOWFLAKE,
    "bigquery": SourceType.BIGQUERY,
    "databricks": SourceType.DATABRICKS,
    "sqlite": SourceType.SQLITE,
    "duckdb": SourceType.SQLITE,
    "s3": SourceType.S3_CSV,
}

_KEYS = {
    "host": "host",
    "port": "port",
    "database": "database",
    "db": "database",
    "schema": "schema_",
    "username": "username",
    "user": "username",
    "password": "password",
    "warehouse": "warehouse",
    "role": "role",
    "account": "account",
    "token": "token",
    "http_path": "http_path",
    "bucket": "bucket",
    "region": "region",
    "key": "key",
    "prefix": "path",
    "path": "path",
}


def guess_type(text: str) -> SourceType | None:
    low = text.lower()
    for word, st in _WORDS.items():
        if word in low:
            return st
    return None


# Whole-word engine mentions. `(?![a-z])` (not \b at the end) is deliberate:
# `_` is a word character, so \bclickhouse\b would MISS clickhouse_connect —
# a module name that is exactly the evidence we want — while (?![a-z]) still
# rejects extensions like "clickhousedb" or "postgresified".
_ENGINE_WORD_RE = re.compile(
    r"\b(clickhouse|postgres(?:ql)?|mariadb|mysql|snowflake|bigquery|"
    r"databricks|mssql|sqlserver|sqlite|duckdb|s3)(?![a-z])",
    re.I,
)


def engine_word_guess(text: str) -> SourceType | None:
    """The engine *named* in prose/code — only when exactly one family appears.

    A weak signal (a comment mentioning an engine is not a connection), so its
    standard is strict: distinct families in the text must be exactly one.
    Two different engines mentioned, or none, returns None — a wrong prefill
    typed into the form by a user is worse than an empty field.
    """
    if not text:
        return None
    families: set[SourceType] = set()
    for m in _ENGINE_WORD_RE.finditer(text):
        st = _WORDS.get(m.group(1).lower()) or _SCHEME_MAP.get(m.group(1).lower())
        if st is not None:
            families.add(st)
    return next(iter(families)) if len(families) == 1 else None


_GET_CLIENT_RE = re.compile(r"clickhouse_connect\s*\.\s*get_client\s*\(([^)]*)\)", re.I)
_HOST_RE = re.compile(r"\bhost\s*=\s*(?P<q>['\"])(?P<v>[^'\"]+)(?P=q)")
_PORT_RE = re.compile(r"\bport\s*=\s*(?P<v>\d{1,5})\b")
_STR_KW_RE = re.compile(r"\b(?P<k>database|username|user)\s*=\s*(?P<q>['\"])(?P<v>[^'\"]+)(?P=q)")


def _client_call_specs(text: str) -> list[ConnectionSpec]:
    """Connection specs encoded in code as clickhouse_connect.get_client(...) calls.

    Only *literal* values are trusted — an unquoted `host=CH_HOST` is a runtime
    variable whose value we cannot know and must not guess. Passwords are not
    captured: specs derived from code carry host/port/db/user, which are
    safe to echo back; secrets stay in the user's own codebase.
    """
    specs: list[ConnectionSpec] = []
    for m in _GET_CLIENT_RE.finditer(text):
        body = m.group(1)
        host = _HOST_RE.search(body)
        if host is None:  # a host is the minimum for a usable connection
            continue
        port = _PORT_RE.search(body)
        spec = ConnectionSpec(
            type=SourceType.CLICKHOUSE,
            name=f"clickhouse-{host.group('v')}",
            host=host.group("v"),
            port=int(port.group("v")) if port else None,
            username=None,
        )
        for kw in _STR_KW_RE.finditer(body):
            key, value = kw.group("k"), kw.group("v")
            if key in ("database",):
                spec.database = value
            else:
                spec.username = value
        specs.append(spec)
    return specs


def parse_url(url: str) -> ConnectionSpec | None:
    m = _URL_RE.search(url.strip())
    if not m:
        return None
    scheme = m.group("scheme").lower()
    st = _SCHEME_MAP.get(scheme)
    if st is None:
        return None

    params = {}
    if m.group("params"):
        for pair in m.group("params").split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                params[k.lower()] = v

    spec = ConnectionSpec(
        type=st,
        name=f"{st}-{m.group('host')}",
        host=m.group("host"),
        port=int(m.group("port")) if m.group("port") else None,
        database=_clean_db(m.group("db")),
        username=m.group("user"),
        password=m.group("pw"),
    )
    for k, v in params.items():
        attr = _KEYS.get(k)
        if attr:
            if getattr(spec, attr, None) in (None, ""):
                setattr(spec, attr, v)
        else:
            # unknown params are still evidence (`secure=true` on clickhouse
            # URLs, driver knobs on any other); they never override known fields
            spec.options.setdefault(k, v)
    return spec


def parse_json_block(text: str) -> ConnectionSpec | None:
    for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if "type" in data and isinstance(data["type"], str):
            try:
                return ConnectionSpec.model_validate(data)
            except Exception:  # noqa: BLE001
                continue
    return None


_FILLER = {
    "is",
    "was",
    "are",
    "the",
    "a",
    "an",
    "at",
    "on",
    "in",
    "to",
    "and",
    "or",
    "as",
    "with",
    "equals",
    "here",
    "that",
    "this",
    "my",
    "our",
    "but",
    "so",
}


def _kv(text: str, keys: str) -> str | None:
    """Find `key=value`, `key: value` or `key value` (optionally via "is")."""
    pattern = rf"\b(?:{keys})\b\s*(?:is|are|was|equals)?\s*[:=]?\s*([^\s,;]+)"
    for m in re.finditer(pattern, text, re.I):
        value = m.group(1).strip("\"'`,")
        if value.lower() in _FILLER:
            continue
        return value
    return None


def extract_connections(text: str) -> list[ConnectionSpec]:
    """Pull every connection spec out of free-form chat text."""
    found: dict[str, ConnectionSpec] = {}

    js = parse_json_block(text)
    if js is not None:
        found[js.name] = js

    for url in _URL_RE.finditer(text):
        spec = parse_url(url.group(0))
        if spec is not None:
            found.setdefault(spec.name, spec)

    # client constructor calls: clickhouse_connect.get_client(host=..., ...)
    for spec in _client_call_specs(text):
        if spec.name not in found:
            found[spec.name] = spec

    # bare host/port/db/user triples
    hint = guess_type(text)
    if hint is not None and not found:
        host = _kv(text, r"host(?:name)?|server")
        port = _kv(text, r"port")
        user = _kv(text, r"user(?:name)?|user\s?id|login")
        pw = _kv(text, r"password|passwd|pwd|secret")
        db = _kv(text, r"database|dbname|db")
        if host or db:
            name = f"{hint}-chat"
            found[name] = ConnectionSpec(
                type=hint,
                name=name,
                host=host,
                port=int(port) if port and port.isdigit() else None,
                database=db,
                username=user,
                password=pw,
            )

    warehouse_hint = guess_type(text)
    if warehouse_hint in {SourceType.SNOWFLAKE, SourceType.BIGQUERY, SourceType.DATABRICKS}:
        acct = _kv(text, r"account")
        wh = _kv(text, r"warehouse")
        token = _kv(text, r"token|pat")
        db = _kv(text, r"database|catalog|project")
        if acct or token or db:
            found.setdefault(
                f"{warehouse_hint}-chat",
                ConnectionSpec(
                    type=warehouse_hint,
                    name=f"{warehouse_hint}-chat",
                    account=acct,
                    warehouse=wh,
                    token=token,
                    database=db,
                ),
            )

    return list(found.values())


# --------------------------------------------------------------------------- #
# LLM-assisted extraction
# --------------------------------------------------------------------------- #

_LLM_PROMPT = """\
Extract every database connection mentioned in the text below and return a
JSON array (no markdown fences, no prose outside the JSON).  Each element
must be a valid ConnectionSpec object with at least a "type" field chosen
from: postgres, mysql, snowflake, bigquery, databricks, mssql, clickhouse,
sqlite, s3_csv.  Include any of the following fields you can identify:
  type, name, host, port, database, schema, username, password,
  account, warehouse, role, http_path, token, bucket, region, key.
If there are no connections, return an empty array [].

Text:
{text}

JSON array:"""


async def extract_connections_async(text: str) -> list[ConnectionSpec]:
    """Extract connection specs from *text*, falling back to the LLM if needed.

    Pipeline:
      1. Try deterministic regex / JSON-block parsing (``extract_connections``).
      2. If that yields nothing **and** an LLM is configured, ask the model to
         emit a structured JSON array of ConnectionSpec objects.

    The function is always safe to call: when no LLM is available it just
    returns the deterministic results (possibly empty).
    """
    # 1. Fast deterministic path
    specs = extract_connections(text)
    if specs:
        return specs

    # 2. LLM fallback
    from app.core.llm import (  # local import to avoid circular deps
        ainvoke,
        llm_available,
        thinking_disabled,
    )

    if not llm_available():
        return []

    try:
        raw = await ainvoke(
            _LLM_PROMPT.format(text=text[:4000]), max_tokens=400, **thinking_disabled()
        )
    except Exception:  # noqa: BLE001
        log.warning("LLM connection extraction failed", exc_info=True)
        return []

    # Strip optional markdown code-fences the model may include anyway
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.I)

    try:
        items = json.loads(raw)
        if not isinstance(items, list):
            items = [items]
    except json.JSONDecodeError:
        log.warning("LLM returned non-JSON for connection extraction: %s", raw[:200])
        return []

    results: list[ConnectionSpec] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        # Auto-generate a name when the LLM didn't include one
        if not item.get("name"):
            item["name"] = f"{item.get('type', 'conn')}-llm"
        try:
            results.append(ConnectionSpec.model_validate(item))
        except Exception:  # noqa: BLE001
            log.debug("LLM connection item failed validation: %s", item)
    return results


_SQL_HINT = re.compile(
    r"\b(select\b.+\bfrom\b|insert\s+into|create\s+(?:or\s+replace\s+)?table|merge\s+into|"
    r"create\s+view|with\s+\w+\s+as\s*\()",
    re.I | re.S,
)
_PY_HINT = re.compile(r"^\s*(import|from)\s+\w+|def\s+\w+\s*\(|read_sql|to_sql|spark\.sql", re.M)
_DBT_HINT = re.compile(r"\{\{\s*(ref|source|config)\s*\(|dbt_version|resource_type", re.I)


def guess_language(text: str) -> EtlLanguage:
    if _DBT_HINT.search(text):
        return EtlLanguage.DBT
    if _PY_HINT.search(text):
        return EtlLanguage.PYTHON
    if _SQL_HINT.search(text):
        return EtlLanguage.SQL
    return EtlLanguage.SQL


def looks_like_code(text: str, min_len: int = 40) -> bool:
    return len(text.strip()) >= min_len and bool(
        _SQL_HINT.search(text) or _PY_HINT.search(text) or _DBT_HINT.search(text)
    )


# --------------------------------------------------------------------------- #
# templated YAML values
# --------------------------------------------------------------------------- #

# a whole-field Jinja placeholder whose env_var has a default literal
_ENV_VAR_DEFAULT_RE = re.compile(
    r"^\{\{\s*env_var\(\s*['\"]\w+['\"]\s*,\s*(?:default\s*=\s*)?['\"](?P<default>[^'\"]*)['\"]\s*\)"
    r"(?:\s*\|\s*(?:int|as_number|as_text|string))?\s*\}\}$",
    re.I,
)


def jinja_value(raw: object) -> object | None:
    """The best literal a templated dbt YAML value can honestly yield.

    dbt projects templatise everything: `port: "{{ env_var('PG_PORT', '5432') }}"`.
    We cannot evaluate the template (the environment belongs to the user's
    shell, not to us), but an `env_var` *default* is a literal in the file and
    safe to recover. Anything else inside `{{ }}` — no default, a conditional,
    a macro — returns None: a dropped hint is honest, a raw template string
    rendered as "the host" is a lie the UI would pre-fill with garbage.
    """
    if not isinstance(raw, str):
        return raw  # ints/bools from YAML, or None
    text = raw.strip().strip("'\"")
    if "{{" not in text:
        return text or None
    m = _ENV_VAR_DEFAULT_RE.match(text)
    return m.group("default").strip() or None if m else None


def coerce_port(raw: object) -> int | None:
    """Port as int, from YAML ints, strings, or env_var defaults."""
    value = jinja_value(raw)
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        port = int(str(value).strip())
    except ValueError:
        return None
    return port if 1 <= port <= 65535 else None
