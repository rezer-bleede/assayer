from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.logging import log
from app.parsers import analyze_etl
from app.schemas import EtlAnalysis, EtlLanguage, EtlSubmit, SourceRequirement
from app.services.repo_fetch import RepoError, fetch
from app.services.source_filter import assemble_sources, profile_summary
from app.store import repository as repo

router = APIRouter(prefix="/api/etl", tags=["etl"])


class AnalyzeResponse(BaseModel):
    analysis: EtlAnalysis
    required_sources: list[SourceRequirement]


def _detect_language(files: dict[str, str], manifest: dict | None) -> EtlLanguage:
    if manifest or any(k.endswith(("dbt_project.yml", "packages.yml")) for k in files):
        return EtlLanguage.DBT
    if any(k.endswith(".py") for k in files):
        sql_only = all(k.endswith((".sql", ".ddl")) for k in files)
        return EtlLanguage.PYTHON if not sql_only else EtlLanguage.SQL
    return EtlLanguage.SQL


@router.post("/analyze", response_model=AnalyzeResponse)
def analyze(req: EtlSubmit, session_id: str | None = None) -> AnalyzeResponse:
    try:
        analysis = analyze_etl(req)
    except Exception as exc:  # noqa: BLE001
        log.warning("etl analysis failed", error=str(exc))
        raise HTTPException(400, f"could not parse ETL: {exc}") from exc
    repo.save_etl(analysis, session_id)
    return AnalyzeResponse(analysis=analysis, required_sources=analysis.required_sources)


class RepoRequest(BaseModel):
    url: str = Field(min_length=6, max_length=400)
    ref: str | None = Field(default=None, max_length=120)
    token: str | None = Field(default=None, max_length=200)
    username: str | None = Field(default=None, max_length=120)
    session_id: str | None = None


@router.post("/from-repo", response_model=AnalyzeResponse)
async def from_repo(req: RepoRequest) -> AnalyzeResponse:
    """Clone a repository and analyse it as one codebase.

    Cloning is blocking and network-bound, so it runs in a worker thread with a
    hard timeout rather than on the event loop.
    """
    if not settings.repo_enabled:
        raise HTTPException(400, "Repository intake is disabled (REPO_ENABLED=false).")

    try:
        content = await asyncio.to_thread(
            fetch, req.url, req.ref, req.token, req.username
        )
    except RepoError as exc:
        # RepoError messages are already scrubbed of credentials
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        from app.services.repo_fetch import scrub

        log.warning("repo fetch failed", error=str(exc))
        raise HTTPException(502, f"could not fetch repository: {scrub(str(exc))}") from exc

    language = _detect_language(content.files, content.manifest)
    analysis = analyze_etl(
        EtlSubmit(
            language=language,
            files=content.files,
            manifest=content.manifest,
            data_profiles=content.data_profiles,
            label=content.repo_url,
            origin="git",
            repo_url=content.repo_url,
            repo_ref=content.ref,
        )
    )
    analysis.risks = [*content.warnings, *analysis.risks]
    repo.save_etl(analysis, req.session_id)
    log.info(
        "repo analysed",
        url=content.repo_url,
        files=len(analysis.files),
        requirements=len(analysis.required_sources),
    )
    return AnalyzeResponse(analysis=analysis, required_sources=analysis.required_sources)


@router.post("/upload", response_model=AnalyzeResponse)
async def upload(
    session_id: str | None = Form(default=None),
    mode: str = Form(
        default="folder",
        description="`folder` for a directory pick/drop (strict filtering); "
        "`files` when the user named individual files, which are taken at "
        "face value — only binaries and hard size caps still apply",
    ),
    files: list[UploadFile] = File(default_factory=list),
    paths: str | None = Form(
        None,
        description="JSON array of relative paths matching `files` (folder uploads "
        "keep their structure so dbt models/seeds/profiles are recognised)",
    ),
) -> AnalyzeResponse:
    """Analyse uploaded source files — individually or as a whole folder.

    Code and config are read and parsed for lineage. Data files (CSV, JSON
    arrays, NDJSON, parquet) are profiled into column metadata instead: the
    rows never enter the analysis, only the table name and its columns do.
    Irrelevant members (VCS/dependency dirs, binaries, lock files, oversized or
    generated files) are skipped rather than fatal, and summarised back in the
    analysis risks so the user can see what was left out.
    """
    if not files:
        raise HTTPException(400, "no files uploaded")
    trust = mode.strip().lower() == "files"

    try:
        rel_paths: list[str] | None = json.loads(paths) if paths else None
    except json.JSONDecodeError as exc:
        raise HTTPException(400, f"invalid paths payload: {exc}") from exc

    raw_items: list[tuple[str, bytes]] = []
    for i, f in enumerate(files):
        raw = await f.read()
        rel = (
            rel_paths[i]
            if rel_paths and i < len(rel_paths) and rel_paths[i]
            else (f.filename or f"upload-{i}.txt")
        )
        raw_items.append((rel.replace("\\", "/"), raw))

    intake = assemble_sources(
        raw_items,
        max_files=settings.repo_max_files,
        max_file_bytes=settings.intake_code_max_bytes,
        data_max_bytes=settings.intake_data_max_bytes,
        max_total_bytes=settings.intake_total_max_bytes,
        max_data_files=settings.intake_data_max_files,
        trust=trust,
    )
    contents, manifest = intake.contents, intake.manifest

    if not contents and not manifest:
        profiled = profile_summary(intake.profiles)
        raise HTTPException(
            400,
            "no readable ETL source found — everything uploaded was filtered out "
            "(dependencies, binaries or unsupported file types)."
            + (f" {profiled}" if profiled else "")
            + " Select the project folder or the .sql / .py / dbt YAML files themselves.",
        )

    language = _detect_language(contents, manifest)
    analysis = analyze_etl(
        EtlSubmit(
            language=language,
            files=contents,
            manifest=manifest,
            data_profiles=intake.profiles,
            label=_project_label(contents),
            origin="upload",
        )
    )
    analysis.risks = [*intake.notes, *analysis.risks]
    repo.save_etl(analysis, session_id)
    log.info(
        "upload analysed",
        uploaded=len(raw_items),
        kept=len(analysis.files),
        profiled=len(intake.profiles),
        requirements=len(analysis.required_sources),
    )
    return AnalyzeResponse(analysis=analysis, required_sources=analysis.required_sources)


def _project_label(contents: dict[str, str]) -> str:
    """Human name for the upload: the folder when one was sent, else the file."""
    for rel in contents:
        parts = rel.split("/")
        if len(parts) > 1:
            return parts[0]
    return next(iter(contents), "upload")


@router.get("/{etl_id}", response_model=EtlAnalysis)
def get_etl(etl_id: str) -> EtlAnalysis:
    a = repo.get_etl(etl_id)
    if not a:
        raise HTTPException(404, "etl analysis not found")
    return a