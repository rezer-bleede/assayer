"""Shared domain models for the data quality agent."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, PlainSerializer, SecretStr


def _unmask(secret: SecretStr | None) -> str | None:
    """Persist the real secret.

    `SecretStr.model_dump(mode="json")` yields `"**********"` by design, which
    would silently store an unusable credential. We unwrap explicitly at the
    persistence boundary; `redacted()` is the only path back to a client.
    """
    return secret.get_secret_value() if secret is not None else None


StoredSecret = Annotated[
    SecretStr | None,
    PlainSerializer(_unmask, return_type=str | None, when_used="json"),
]


def new_id() -> str:
    return uuid.uuid4().hex


def now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #


class SourceType(StrEnum):
    POSTGRES = "postgres"
    MYSQL = "mysql"
    SNOWFLAKE = "snowflake"
    BIGQUERY = "bigquery"
    DATABRICKS = "databricks"
    MSSQL = "mssql"
    SQLITE = "sqlite"
    S3_CSV = "s3_csv"
    LOCAL_CSV = "local_csv"


class ConnectionSpec(BaseModel):
    """Everything needed to reach a data source. Secrets are never echoed back."""

    type: SourceType
    name: str = Field(min_length=1, max_length=64)
    host: str | None = None
    port: int | None = None
    database: str | None = None
    schema_: str | None = Field(default=None, alias="schema")
    username: str | None = None
    password: StoredSecret = None
    # warehouse/session extras
    warehouse: str | None = None
    role: str | None = None
    account: str | None = None
    http_path: str | None = None
    token: StoredSecret = None
    credentials_json: StoredSecret = None
    # file sources
    bucket: str | None = None
    region: str | None = None
    key: str | None = None
    path: str | None = None
    file_format: str = "csv"
    read_only: bool = True
    options: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True, "serialize_by_alias": True}

    def redacted(self) -> dict[str, Any]:
        d = self.model_dump(mode="json", by_alias=True)
        for k in ("password", "token", "credentials_json"):
            if d.get(k):
                d[k] = "***"
        return d


class ConnectionStatus(BaseModel):
    ok: bool
    type: SourceType
    name: str
    latency_ms: float | None = None
    message: str | None = None
    schemas: list[str] = Field(default_factory=list)
    tables: list[TableInfo] = Field(default_factory=list)
    server_version: str | None = None


class SchemaNamed(BaseModel):
    """Base for models whose schema column must serialize as `schema`.

    `populate_by_name` lets callers pass either `schema=` or `schema_=`;
    `serialize_by_alias` makes the JSON output always use `schema` (the key the
    UI and the DSL use), no matter who calls `model_dump`.
    """

    model_config = {"populate_by_name": True, "serialize_by_alias": True}


class TableInfo(SchemaNamed):
    schema_: str | None = Field(default=None, alias="schema", serialization_alias="schema")
    name: str
    row_count: int | None = None
    columns: list[ColumnInfo] = Field(default_factory=list)


class ColumnInfo(BaseModel):
    name: str
    data_type: str
    nullable: bool = True
    is_primary_key: bool = False


class ConnectionRecord(BaseModel):
    id: str = Field(default_factory=new_id)
    created_at: datetime = Field(default_factory=now)
    spec: ConnectionSpec
    status: ConnectionStatus | None = None


# --------------------------------------------------------------------------- #
# ETL
# --------------------------------------------------------------------------- #


class EtlLanguage(StrEnum):
    SQL = "sql"
    PYTHON = "python"
    DBT = "dbt"


class ColumnProfile(BaseModel):
    """One column as seen in a data file (inferred, not introspected)."""

    name: str
    data_type: str = "string"
    # null_ratio is measured over the sampled rows, not the whole file
    null_ratio: float | None = None
    distinct_values: int | None = None
    samples: list[str] = Field(default_factory=list)


class DataProfile(BaseModel):
    """Column-level metadata for a data file the user handed us.

    The file's rows never enter the analysis: only this profile does. That is
    what lets a 40 MB seed CSV cost a few KB of context instead of a context
    window, while still telling us the table exists and what it looks like.
    """

    path: str
    table: str
    format: str = "csv"
    row_count: int | None = None
    sampled_rows: int = 0
    size_bytes: int = 0
    columns: list[ColumnProfile] = Field(default_factory=list)
    truncated: bool = False
    note: str | None = None

    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    def describe(self) -> str:
        """One clause for chat: `raw_customers.csv — 120 rows, id:integer, name:text`."""
        cols = ", ".join(f"{c.name}:{c.data_type}" for c in self.columns[:4])
        more = f" +{len(self.columns) - 4} more" if len(self.columns) > 4 else ""
        head = f"{self.path} — "
        if self.row_count is None:
            return f"{head}{len(self.columns)} column(s){more}"
        return f"{head}{self.row_count:,} row(s), {cols}{more}"


class EtlSubmit(BaseModel):
    language: EtlLanguage = EtlLanguage.SQL
    code: str | None = None
    files: dict[str, str] = Field(default_factory=dict)
    manifest: dict[str, Any] | None = None
    data_profiles: list[DataProfile] = Field(default_factory=list)
    label: str | None = None
    # provenance
    origin: str | None = None
    repo_url: str | None = None
    repo_ref: str | None = None


class Transformation(BaseModel):
    kind: str
    description: str
    source: str | None = None
    target: str | None = None
    fields: list[str] = Field(default_factory=list)
    expression: str | None = None
    confidence: float = 0.5


class LineageEdge(BaseModel):
    source: str
    target: str


class EtlOrigin(StrEnum):
    GIT = "git"
    UPLOAD = "upload"
    PASTE = "paste"
    UNKNOWN = "unknown"


class SourceRequirement(BaseModel):
    """One connection the ETL needs, grouped by inferred database.

    The agent asks exactly one question per requirement, pre-filled from
    whatever evidence the code gives up (dbt profiles, engine URLs in the code,
    schema prefixes).
    """

    key: str
    tables: list[str] = Field(default_factory=list)
    database: str | None = None
    schema_: str | None = Field(default=None, alias="schema", serialization_alias="schema")
    engine_hint: SourceType | None = None
    host_hint: str | None = None
    port_hint: int | None = None
    user_hint: str | None = None
    role: Literal["source", "target", "mixed"] = "source"
    origin: str = "sql schema prefix"
    confidence: float = 0.5

    model_config = {"populate_by_name": True}

    @property
    def is_target_side(self) -> bool:
        return self.role in ("target", "mixed")

    def describe(self) -> str:
        bits = [f"{len(self.tables)} table(s)"]
        if self.database:
            bits.append(f"database `{self.database}`")
        if self.engine_hint:
            bits.append(str(self.engine_hint))
        if self.host_hint:
            bits.append(f"host `{self.host_hint}`")
        return ", ".join(bits)


class EtlAnalysis(BaseModel):
    id: str = Field(default_factory=new_id)
    language: EtlLanguage
    label: str | None = None
    transformations: list[Transformation] = Field(default_factory=list)
    lineage: list[LineageEdge] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    targets: list[str] = Field(default_factory=list)
    joins: list[dict[str, Any]] = Field(default_factory=list)
    filters: list[dict[str, Any]] = Field(default_factory=list)
    aggregations: list[dict[str, Any]] = Field(default_factory=list)
    primary_sources: list[str] = Field(default_factory=list)
    # target table -> the single table whose grain it inherits (parity baseline)
    grain: dict[str, str] = Field(default_factory=dict)
    risks: list[str] = Field(default_factory=list)
    raw_code: str = ""
    origin: EtlOrigin = EtlOrigin.UNKNOWN
    # repo provenance; credentials are always stripped
    repo_url: str | None = None
    repo_ref: str | None = None
    files: list[str] = Field(default_factory=list)
    # redacted connection strings discovered inside the code itself
    connection_hints: list[dict[str, Any]] = Field(default_factory=list)
    # per-table metadata ({database, schema, engine}) that survives parsing
    table_hints: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # column metadata for data files (seeds, CSV drops, JSON/parquet exports)
    data_profiles: list[DataProfile] = Field(default_factory=list)
    required_sources: list[SourceRequirement] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=now)


# --------------------------------------------------------------------------- #
# Conversation: intake -> analyze -> sources -> plan -> approve -> execute
# --------------------------------------------------------------------------- #


class ConversationStage(StrEnum):
    NEED_ETL = "need_etl"
    ANALYZING = "analyzing"
    NEED_SOURCES = "need_sources"
    READY = "ready"
    AWAITING_APPROVAL = "awaiting_approval"
    RUNNING = "running"
    DONE = "done"


class QuestionKind(StrEnum):
    GIT_URL = "git_url"
    FILE_UPLOAD = "file_upload"
    PASTE = "paste"
    CONNECTION = "connection"


class QuestionOption(BaseModel):
    kind: QuestionKind
    label: str
    help: str | None = None
    placeholder: str | None = None
    prefill: dict[str, Any] = Field(default_factory=dict)
    fields: list[str] = Field(default_factory=list)
    preferred: bool = True
    """False for `paste`: the user should be steered to repo or upload first."""


class Question(BaseModel):
    """A server-driven prompt, rendered by the UI with no LLM required."""

    id: str = Field(default_factory=new_id)
    stage: ConversationStage
    title: str
    prompt: str
    options: list[QuestionOption] = Field(default_factory=list)
    requirement_key: str | None = None
    skip_allowed: bool = False
    created_at: datetime = Field(default_factory=now)


# --------------------------------------------------------------------------- #
# Test plan
# --------------------------------------------------------------------------- #


class CheckKind(StrEnum):
    SCHEMA = "schema"
    NULL_RATE = "null_rate"
    UNIQUE = "unique"
    DUPLICATE = "duplicate"
    REFERENTIAL = "referential"
    ROW_COUNT = "row_count"
    FRESHNESS = "freshness"
    MIN_MAX = "min_max"
    RANGE = "range"
    PATTERN = "pattern"
    DISTRIBUTION = "distribution"
    CUSTOM_SQL = "custom_sql"
    VOLUME = "volume"


class Severity(StrEnum):
    BLOCKER = "blocker"
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class CheckStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


class TableRef(SchemaNamed):
    source_id: str
    schema_: str | None = Field(default=None, alias="schema", serialization_alias="schema")
    name: str

    @property
    def fqn(self) -> str:
        return f"{self.schema_}.{self.name}" if self.schema_ else self.name


class CheckSpec(BaseModel):
    id: str = Field(default_factory=new_id)
    name: str
    kind: CheckKind
    table: TableRef
    column: str | None = None
    severity: Severity = Severity.CRITICAL
    # threshold semantics depend on kind:
    #   null_rate  -> max allowed null fraction (0..1)
    #   unique/duplicate -> informational, no threshold
    #   referential -> no threshold (count of orphans)
    #   row_count/volume -> tolerance_ratio
    #   min_max/range -> min_value/max_value
    #   pattern -> regex
    #   custom_sql -> sql + expected (bool)
    threshold: float | None = None
    min_value: float | str | None = None
    max_value: float | str | None = None
    tolerance_ratio: float = 0.0
    regex: str | None = None
    sql: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    rationale: str | None = None
    tags: list[str] = Field(default_factory=list)


class TestPlan(BaseModel):
    id: str = Field(default_factory=new_id)
    run_id: str | None = None
    summary: str | None = None
    checks: list[CheckSpec] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=now)


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


class CheckResult(BaseModel):
    check_id: str
    name: str
    kind: CheckKind
    table: TableRef
    column: str | None = None
    severity: Severity = Severity.CRITICAL
    status: CheckStatus = CheckStatus.PENDING
    observed: float | str | None = None
    expected: float | str | None = None
    failed_rows: int | None = None
    sample_rows: list[dict[str, Any]] = Field(default_factory=list)
    query: str | None = None
    duration_ms: float | None = None
    message: str | None = None


class QualityScore(BaseModel):
    overall: float = 0.0
    by_severity: dict[str, float] = Field(default_factory=dict)
    passed: int = 0
    failed: int = 0
    errored: int = 0
    skipped: int = 0


class RunStatus(StrEnum):
    PENDING = "pending"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunResult(BaseModel):
    id: str
    status: RunStatus = RunStatus.PENDING
    plan: TestPlan | None = None
    analysis: EtlAnalysis | None = None
    results: list[CheckResult] = Field(default_factory=list)
    score: QualityScore = Field(default_factory=QualityScore)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: float | None = None
    narrative: str | None = None


# --------------------------------------------------------------------------- #
# Chat
# --------------------------------------------------------------------------- #


class ChatRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class ChatAttachment(BaseModel):
    kind: Literal["connection", "etl", "plan", "result"]
    ref_id: str
    label: str | None = None


class ChatMessage(BaseModel):
    id: str = Field(default_factory=new_id)
    role: ChatRole
    content: str
    attachments: list[ChatAttachment] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=now)


class ChatState(BaseModel):
    session_id: str = Field(default_factory=new_id)
    messages: list[ChatMessage] = Field(default_factory=list)
    connections: list[ConnectionSpec] = Field(default_factory=list)
    etl: EtlAnalysis | None = None
    plan: TestPlan | None = None
    run_id: str | None = None


class StreamEvent(BaseModel):
    type: str
    session_id: str
    run_id: str | None = None
    ts: float = Field(default_factory=time.time)
    data: dict[str, Any] = Field(default_factory=dict)