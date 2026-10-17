"""Ingestion of connection info and ETL code supplied via chat text.

This is what lets the user "just paste everything in the chat window".
"""

from __future__ import annotations

import json
import re

from app.schemas import ConnectionSpec, EtlLanguage, SourceType

_URL_RE = re.compile(
    r"(?P<scheme>[a-z0-9+]+)://"
    r"(?:(?P<user>[^:/@\s]+)(?::(?P<pw>[^@\s]+))?@)?"
    r"(?P<host>[^:/?@\s]+)(?::(?P<port>\d+))?"
    r"(?:/(?P<db>[^?\s]*))?"
    r"(?:\?(?P<params>\S+))?",
    re.I,
)

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
}

_WORDS = {
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
        database=m.group("db") or None,
        username=m.group("user"),
        password=m.group("pw"),
    )
    for k, v in params.items():
        attr = _KEYS.get(k)
        if attr and getattr(spec, attr, None) in (None, ""):
            setattr(spec, attr, v)
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
    "is", "was", "are", "the", "a", "an", "at", "on", "in", "to", "and", "or",
    "as", "with", "equals", "here", "that", "this", "my", "our", "but", "so",
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
    r"^\{\{\s*env_var\(\s*['\"]\w+['\"]\s*,\s*['\"](?P<default>[^'\"]*)['\"]\s*\)"
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
