from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- app ---
    app_name: str = "assayer"
    environment: str = "development"
    secret_key: str = "change-me"
    bootstrap_token: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_redirect_uri: str = ""
    web_origin: str = "http://localhost:3010"
    cors_origins: str = "*"
    log_level: str = "INFO"

    # --- state store ---
    database_url: str = "postgresql+psycopg://assayer:assayer@db:5432/assayer"
    redis_url: str = "redis://redis:6379/0"

    # --- llm ---
    # The default is the local model runner that ships with the stack, so a
    # plain `docker compose up` has a working LLM with nothing to configure.
    # Point these at any OpenAI-compatible endpoint (OpenAI, Azure, vLLM,
    # LiteLLM) to use something else; an unreachable endpoint degrades to the
    # deterministic planner rather than failing.
    llm_provider: str = "ollama"
    llm_base_url: str | None = "http://ollama:11434/v1"
    llm_api_key: str | None = None
    llm_model: str = "qwen2.5-coder:3b"
    llm_temperature: float = 0.1
    # Seconds for one model call before it is abandoned. A slow local model on
    # a CPU-only host must not stall the agent for minutes: the deterministic
    # planner picks up as soon as this elapses.
    llm_timeout_seconds: int = 90
    # A thinking model can stream reasoning for minutes. Silence longer than
    # this (a wedged runner, a dropped stream) ends the call with whatever was
    # generated, so the deterministic fallback can take over instead of the
    # turn hanging until the wall-clock timeout.
    llm_stream_stall_seconds: int = 120
    # Retries after the first attempt. Zero by default — with a good fallback,
    # retrying a slow or unreachable endpoint only delays the inevitable.
    llm_max_retries: int = 0

    # --- OpenUI authoring (AG-UI surface) ---
    # The model writes the OpenUI Lang program; the deterministic serializer
    # is the fallback when it is slow, invalid, or unavailable.
    openui_author_enabled: bool = True
    openui_author_max_tokens: int = 1400

    # --- run limits ---
    run_max_checks: int = 200
    run_query_timeout_seconds: int = 300
    run_max_row_sample: int = 20
    run_max_concurrency: int = 4

    # --- browser UI checks ---
    ui_enabled: bool = True
    ui_check_timeout_seconds: int = 30
    ui_nav_timeout_seconds: int = 15
    ui_max_steps: int = 25
    ui_max_pages: int = 12
    ui_artifact_dir: str = "/tmp/assayer-ui-artifacts"
    ui_allow_private_hosts: bool = True
    ui_visual_default_tolerance: float = 0.005

    # --- connection repair: auto-retry a failed connection with better guesses ---
    # The deterministic ladder is free; the LLM rounds cost tokens and seconds,
    # so both the attempt count and the wall clock are bounded. "As many tries
    # as needed" is honoured *within* these limits, and the cap is disclosed.
    connection_repair_enabled: bool = True
    connection_repair_max_attempts: int = 6
    connection_repair_max_llm_rounds: int = 3
    connection_repair_timeout_seconds: float = 45.0

    # --- git intake: cloning is network-bound and unbounded by default ---
    repo_enabled: bool = True
    repo_max_files: int = 400
    repo_max_file_bytes: int = 256_000
    repo_max_total_bytes: int = 4_000_000
    repo_clone_timeout_seconds: int = 120
    repo_tmp_dir: str = "/tmp/assayer-repos"
    # RFC1918 is allowed by default (internal GitLab/GitHub Enterprise are the
    # common case), but cloud metadata + loopback are always refused.
    repo_allow_private_hosts: bool = True

    # --- source intake (folder upload + git clone share these) ---
    # Code/config we are willing to read: a model above this is generated or
    # machine-written and carries no readable lineage.
    intake_code_max_bytes: int = 131_072
    # Data files are profiled, not read as source, so they get their own cap —
    # a seed CSV is expected to be far larger than a dbt model.
    intake_data_max_bytes: int = 8_000_000
    intake_data_max_files: int = 50
    intake_total_max_bytes: int = 4_000_000

    @field_validator("llm_base_url", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("llm_api_key", mode="before")
    @classmethod
    def _blank_key_to_none(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @property
    def cors_origin_list(self) -> list[str]:
        raw = (self.cors_origins or "*").strip()
        if raw == "*":
            return ["*"]
        return [o.strip() for o in raw.split(",") if o.strip()]

    @property
    def llm_configured(self) -> bool:
        if self.llm_provider.lower() == "ollama":
            return bool(self.llm_base_url)
        return bool(self.llm_api_key and self.llm_base_url)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
