from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.logging import log
from app.parsers import analyze_etl
from app.schemas import EtlAnalysis, EtlLanguage, EtlSubmit, SourceRequirement
from app.services.repo_fetch import RepoError, fetch
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
    files: list[UploadFile] = File(default_factory=list),
) -> AnalyzeResponse:
    """Analyse uploaded source files. Binary members are skipped, not fatal."""
    if not files:
        raise HTTPException(400, "no files uploaded")

    contents: dict[str, str] = {}
    manifest: dict[str, Any] | None = None
    skipped: list[str] = []

    for f in files:
        raw = await f.read()
        name = f.filename or "upload.txt"
        if len(raw) > settings.repo_max_file_bytes:
            skipped.append(f"{name} (too large)")
            continue
        text = raw.decode("utf-8", errors="replace")
        if name.endswith("manifest.json"):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict) and "nodes" in parsed:
                    manifest = parsed
                    continue
            except json.JSONDecodeError:
                skipped.append(f"{name} (invalid JSON)")
                continue
        contents[name] = text

    if not contents and not manifest:
        raise HTTPException(400, f"no readable source files ({', '.join(skipped) or 'none provided'})")

    language = _detect_language(contents, manifest)
    analysis = analyze_etl(
        EtlSubmit(
            language=language,
            files=contents,
            manifest=manifest,
            label=next(iter(contents), "upload"),
            origin="upload",
        )
    )
    analysis.risks = [*skipped, *analysis.risks]
    repo.save_etl(analysis, session_id)
    return AnalyzeResponse(analysis=analysis, required_sources=analysis.required_sources)


@router.get("/{etl_id}", response_model=EtlAnalysis)
def get_etl(etl_id: str) -> EtlAnalysis:
    a = repo.get_etl(etl_id)
    if not a:
        raise HTTPException(404, "etl analysis not found")
    return a