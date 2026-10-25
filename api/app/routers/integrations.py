"""Connected services and preview-first integration actions."""

from __future__ import annotations

import secrets
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.identity import current_user_id, current_workspace_id
from app.core.routes import SafeAPIRoute
from app.services import integrations as service

router = APIRouter(prefix="/api/integrations", tags=["integrations"], route_class=SafeAPIRoute)


class BindRepository(BaseModel):
    integration_id: str
    repository: str = Field(min_length=3, max_length=255)


class PrepareIssue(BaseModel):
    integration_id: str
    run_id: str
    repository: str
    title: str = Field(min_length=1, max_length=240)
    body: str = Field(default="", max_length=12_000)


class ConfirmAction(BaseModel):
    title: str | None = Field(default=None, max_length=240)
    body: str | None = Field(default=None, max_length=12_000)


@router.get("")
def list_integrations() -> dict[str, Any]:
    return {"integrations": service.list_integrations()}


@router.get("/github/connect")
def github_connect(request: Request) -> RedirectResponse:
    if not current_user_id() or not current_workspace_id():
        raise HTTPException(401, "sign in to connect GitHub")
    if not settings.github_client_id or not settings.github_client_secret:
        raise HTTPException(503, "GitHub integration is not configured by the administrator")
    state = secrets.token_urlsafe(32)
    request.session["github_oauth_state"] = state
    request.session["github_oauth_identity"] = {
        "user_id": current_user_id(),
        "workspace_id": current_workspace_id(),
    }
    callback = settings.github_redirect_uri or str(request.url_for("github_callback"))
    query = urlencode({
        "client_id": settings.github_client_id,
        "redirect_uri": callback,
        "scope": "repo read:user",
        "state": state,
    })
    return RedirectResponse(f"https://github.com/login/oauth/authorize?{query}", status_code=302)


@router.get("/github/callback", name="github_callback")
async def github_callback(request: Request, code: str | None = None, state: str | None = None):
    expected = request.session.pop("github_oauth_state", None)
    expected_identity = request.session.pop("github_oauth_identity", None)
    if not expected or not state or not secrets.compare_digest(expected, state):
        raise HTTPException(400, "GitHub authorization state did not match; start again")
    if (
        not code
        or not current_user_id()
        or not current_workspace_id()
        or expected_identity != {
            "user_id": current_user_id(),
            "workspace_id": current_workspace_id(),
        }
    ):
        raise HTTPException(400, "Your account or workspace changed during GitHub authorization; start again")
    async with httpx.AsyncClient(timeout=20) as client:
        token_response = await client.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            json={"client_id": settings.github_client_id, "client_secret": settings.github_client_secret,
                  "code": code, "redirect_uri": settings.github_redirect_uri or str(request.url_for("github_callback"))},
        )
        if token_response.status_code >= 400:
            raise HTTPException(502, "GitHub token exchange failed")
        token_data = token_response.json()
        access_token = token_data.get("access_token")
        if not access_token:
            raise HTTPException(400, "GitHub authorization was denied")
        user_response = await client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
        )
        if user_response.status_code >= 400:
            raise HTTPException(502, "Could not load the authorized GitHub account")
    account = user_response.json()
    service.save_github_integration(str(access_token), str(account["id"]), str(account.get("login") or "GitHub"))
    response = RedirectResponse(f"{settings.web_origin.rstrip('/')}/?integration=github-connected", status_code=303)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/github/{integration_id}/repositories")
async def github_repositories(integration_id: str) -> dict[str, Any]:
    return {"repositories": await service.list_repositories(integration_id)}


@router.delete("/github/{integration_id}", status_code=204)
async def disconnect_github(integration_id: str) -> None:
    if not await service.delete_integration(integration_id):
        raise HTTPException(404, "GitHub integration not found")


@router.get("/projects/{project_id}/repositories")
def project_repositories(project_id: str) -> dict[str, Any]:
    return {"repositories": service.list_project_repositories(project_id)}


@router.post("/projects/{project_id}/repositories")
async def add_project_repository(project_id: str, body: BindRepository) -> dict[str, str]:
    repositories = await service.list_repositories(body.integration_id)
    if not any(r["full_name"].lower() == body.repository.lower() for r in repositories):
        raise HTTPException(404, "repository is not accessible to this GitHub account")
    return service.bind_project_repository(project_id, body.integration_id, body.repository)


@router.delete("/projects/{project_id}/repositories/{binding_id}", status_code=204)
def delete_project_repository(project_id: str, binding_id: str) -> None:
    if not service.remove_project_repository(project_id, binding_id):
        raise HTTPException(404, "repository binding not found")


@router.post("/github/issues/preview")
def preview_github_issue(body: PrepareIssue) -> dict[str, str]:
    return service.prepare_issue_action(
        body.integration_id, body.run_id, body.repository, body.title, body.body
    )


@router.get("/runs/{run_id}/destinations")
def run_destinations(run_id: str) -> dict[str, Any]:
    return {"destinations": service.destinations_for_run(run_id)}


@router.post("/actions/{action_id}/confirm")
async def confirm_action(action_id: str, body: ConfirmAction) -> dict[str, Any]:
    return await service.confirm_issue_action(action_id, body.title, body.body)
