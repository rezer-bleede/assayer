"""Account, session, workspace, and application preference endpoints."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, update

from app.core.config import settings
from app.schemas import new_id
from app.store import db as db_store
from app.store.db import (
    AppSettingsRow,
    GuestSessionRow,
    LoginSessionRow,
    UserRow,
    WorkspaceInviteRow,
    WorkspaceMemberRow,
    WorkspaceRow,
    utcnow_str,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])
COOKIE = "assayer_session"
SESSION_DAYS = 30
GUEST_COOKIE = "assayer_guest"
GUEST_DAYS = 7
GUEST_MESSAGE_LIMIT = 5


def _password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return f"pbkdf2_sha256${salt.hex()}${digest.hex()}"


def _verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, salt, expected = encoded.split("$", 2)
        return scheme == "pbkdf2_sha256" and hmac.compare_digest(
            _password_hash(password, bytes.fromhex(salt)).rsplit("$", 1)[1], expected
        )
    except (ValueError, TypeError):
        return False


def _user_dict(user: UserRow) -> dict[str, Any]:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.display_name,
        "role": user.role,
        "settings": user.settings or {},
    }


def _oauth_client(provider: str):
    from authlib.integrations.starlette_client import OAuth

    oauth = OAuth()
    if provider == "google":
        oauth.register(
            name="idp", client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )
    else:
        if not settings.oidc_issuer or not settings.oidc_client_id or not settings.oidc_client_secret:
            raise HTTPException(404, "company sign-in is not configured")
        oauth.register(
            name="idp", client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret,
            server_metadata_url=f"{settings.oidc_issuer.rstrip('/')}/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )
    return oauth.create_client("idp")


def _set_login_cookie(user: UserRow, request: Request, response: Response) -> None:
    token = secrets.token_urlsafe(32)
    with db_store.SessionLocal.begin() as db:
        db.add(LoginSessionRow(
            id=new_id(), user_id=user.id, token_hash=hashlib.sha256(token.encode()).hexdigest(),
            expires_at=(datetime.now(UTC) + timedelta(days=SESSION_DAYS)).isoformat(), created_at=utcnow_str(),
        ))
    response.set_cookie(COOKIE, token, httponly=True, secure=request.url.scheme == "https",
                        samesite="lax", max_age=SESSION_DAYS * 86400, path="/")


def current_user(request: Request) -> UserRow:
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "sign in required")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with db_store.SessionLocal() as db:
        row = db.scalar(select(LoginSessionRow).where(LoginSessionRow.token_hash == token_hash))
        if row is None or row.expires_at <= utcnow_str():
            raise HTTPException(401, "session expired")
        user = db.get(UserRow, row.user_id)
        if user is None:
            raise HTTPException(401, "account unavailable")
        return user


class Credentials(BaseModel):
    email: str = Field(min_length=5, max_length=320, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    password: str = Field(min_length=12, max_length=256)


class BootstrapRequest(Credentials):
    name: str = Field(min_length=1, max_length=120)
    setup_token: str = Field(min_length=16, max_length=256)


class SignupRequest(Credentials):
    name: str = Field(min_length=1, max_length=120)


class ProfilePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    settings: dict[str, Any] | None = None


class WorkspaceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: str = "team"


class ActivateWorkspaceRequest(BaseModel):
    workspace_id: str


class InstanceSettingsPatch(BaseModel):
    run_max_checks: int | None = Field(default=None, ge=1, le=2000)
    run_max_concurrency: int | None = Field(default=None, ge=1, le=32)
    run_max_row_sample: int | None = Field(default=None, ge=0, le=100)
    ui_visual_default_tolerance: float | None = Field(default=None, ge=0, le=0.25)


class InviteRequest(BaseModel):
    email: str = Field(min_length=5, max_length=320, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    role: str = Field(default="member", pattern="^member$")


class AcceptInviteRequest(Credentials):
    token: str = Field(min_length=32, max_length=256)
    name: str = Field(min_length=1, max_length=120)


class JoinInviteRequest(BaseModel):
    token: str = Field(min_length=32, max_length=256)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


@router.get("/status")
def status(request: Request, response: Response) -> dict[str, Any]:
    with db_store.SessionLocal() as db:
        has_users = db.scalar(select(UserRow.id).limit(1)) is not None
        token = request.cookies.get(GUEST_COOKIE)
        guest = db.scalar(select(GuestSessionRow).where(
            GuestSessionRow.token_hash == hashlib.sha256(token.encode()).hexdigest()
        )) if token else None
        if guest is None or guest.expires_at <= utcnow_str():
            token = secrets.token_urlsafe(32)
            guest = GuestSessionRow(
                id=new_id(), token_hash=hashlib.sha256(token.encode()).hexdigest(), message_count=0,
                expires_at=(datetime.now(UTC) + timedelta(days=GUEST_DAYS)).isoformat(), created_at=utcnow_str(),
            )
            db.add(guest)
            db.commit()
            response.set_cookie(GUEST_COOKIE, token, httponly=True, secure=request.url.scheme == "https",
                                samesite="lax", max_age=GUEST_DAYS * 86400, path="/")
    return {
        "setup_required": not has_users,
        "google_enabled": bool(settings.google_client_id and settings.google_client_secret),
        "sso_enabled": bool(settings.oidc_issuer and settings.oidc_client_id),
        "authenticated": False,
        "guest": True,
        "messages_remaining": max(0, GUEST_MESSAGE_LIMIT - guest.message_count),
        "message_limit": GUEST_MESSAGE_LIMIT,
    }


@router.post("/guest-message")
def consume_guest_message(request: Request) -> dict[str, int]:
    token = request.cookies.get(GUEST_COOKIE)
    if not token:
        raise HTTPException(401, "guest session required")
    digest = hashlib.sha256(token.encode()).hexdigest()
    with db_store.SessionLocal.begin() as db:
        guest = db.scalar(select(GuestSessionRow).where(GuestSessionRow.token_hash == digest))
        if guest is None or guest.expires_at <= utcnow_str():
            raise HTTPException(401, "guest session expired")
        changed = db.execute(update(GuestSessionRow).where(
            GuestSessionRow.id == guest.id,
            GuestSessionRow.message_count < GUEST_MESSAGE_LIMIT,
        ).values(message_count=GuestSessionRow.message_count + 1)).rowcount
        if not changed:
            raise HTTPException(429, "Sign in or create an account to continue.", headers={"X-Assayer-Login-Required": "true"})
        remaining = GUEST_MESSAGE_LIMIT - guest.message_count - 1
    return {"messages_remaining": remaining}


def _transfer_guest_rows(db, guest_id: str, user_id: str, workspace_id: str) -> None:
    """Move only this browser's guest-owned records into the new account."""
    for table in ("sessions", "connections", "etl_analyses", "runs"):
        db.execute(__import__("sqlalchemy").text(
            f"UPDATE {table} SET user_id=:uid, workspace_id=:wid WHERE user_id=:gid"
        ), {"uid": user_id, "wid": workspace_id, "gid": guest_id})
    for table in ("projects", "project_files"):
        db.execute(__import__("sqlalchemy").text(
            f"UPDATE {table} SET owner_id=:uid, workspace_id=:wid WHERE owner_id=:gid"
        ), {"uid": user_id, "wid": workspace_id, "gid": guest_id})


def _transfer_request_guest(db, request: Request, user_id: str, workspace_id: str) -> None:
    token = request.cookies.get(GUEST_COOKIE)
    if not token:
        return
    digest = hashlib.sha256(token.encode()).hexdigest()
    guest = db.scalar(select(GuestSessionRow).where(GuestSessionRow.token_hash == digest))
    if guest and guest.expires_at > utcnow_str():
        _transfer_guest_rows(db, guest.id, user_id, workspace_id)
        db.delete(guest)


@router.get("/google")
async def google_login(request: Request):
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(404, "Google sign-in is not configured")
    callback = str(request.url_for("google_callback"))
    return await _oauth_client("google").authorize_redirect(request, callback)


@router.get("/google/callback", name="google_callback")
async def google_callback(request: Request):
    return await _finish_oidc(request, "google")


@router.get("/sso")
async def sso_login(request: Request):
    return await _oauth_start_sso(request)


async def _oauth_start_sso(request: Request):
    callback = settings.oidc_redirect_uri or str(request.url_for("sso_callback"))
    return await _oauth_client("sso").authorize_redirect(request, callback)


@router.get("/sso/callback", name="sso_callback")
async def sso_callback(request: Request):
    return await _finish_oidc(request, "sso")


async def _finish_oidc(request: Request, provider: str):
    client = _oauth_client(provider)
    token = await client.authorize_access_token(request)
    claims = token.get("userinfo") or {}
    email = str(claims.get("email") or "").strip().lower()
    if not email or claims.get("email_verified") is not True:
        raise HTTPException(403, "identity provider did not verify the email address")
    with db_store.SessionLocal.begin() as db:
        user = db.scalar(select(UserRow).where(UserRow.email == email))
        if user is None:
            raise HTTPException(403, "account not found; ask your administrator for access")
        workspace = db.scalar(select(WorkspaceRow).join(
            WorkspaceMemberRow, WorkspaceMemberRow.workspace_id == WorkspaceRow.id
        ).where(WorkspaceMemberRow.user_id == user.id, WorkspaceRow.kind == "personal"))
        if workspace:
            _transfer_request_guest(db, request, user.id, workspace.id)
    response = RedirectResponse(settings.web_origin, status_code=303)
    _set_login_cookie(user, request, response)
    response.delete_cookie(GUEST_COOKIE, path="/")
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/bootstrap")
def bootstrap(body: BootstrapRequest, request: Request, response: Response) -> dict[str, Any]:
    expected = settings.bootstrap_token
    if not expected or not hmac.compare_digest(body.setup_token, expected):
        raise HTTPException(403, "invalid setup token")
    with db_store.SessionLocal.begin() as db:
        if db.scalar(select(UserRow.id).limit(1)) is not None:
            raise HTTPException(409, "initial setup is already complete")
        user = UserRow(
            id=new_id(), email=str(body.email).lower(), display_name=body.name.strip(),
            password_hash=_password_hash(body.password), role="admin", created_at=utcnow_str(),
            settings={"theme": "system", "instructions": ""},
        )
        workspace = WorkspaceRow(
            id=new_id(), name=f"{user.display_name}'s workspace", owner_id=user.id,
            kind="personal", settings={}, created_at=utcnow_str(),
        )
        db.add_all([user, workspace])
        db.flush()
        db.add(WorkspaceMemberRow(
            id=new_id(), workspace_id=workspace.id, user_id=user.id, role="owner", created_at=utcnow_str()
        ))
        _transfer_request_guest(db, request, user.id, workspace.id)
        # Adopt pre-login records into the first administrator's workspace.
        for table in ("sessions", "connections", "etl_analyses", "runs"):
            db.execute(__import__("sqlalchemy").text(f"UPDATE {table} SET user_id=:uid WHERE user_id IS NULL"), {"uid": user.id})
        for table in ("projects", "project_files"):
            db.execute(__import__("sqlalchemy").text(f"UPDATE {table} SET owner_id=:uid WHERE owner_id IS NULL"), {"uid": user.id})
        for table in ("sessions", "connections", "etl_analyses", "runs", "projects", "project_files"):
            db.execute(__import__("sqlalchemy").text(f"UPDATE {table} SET workspace_id=:wid WHERE workspace_id IS NULL"), {"wid": workspace.id})
        token = secrets.token_urlsafe(32)
        db.add(LoginSessionRow(
            id=new_id(), user_id=user.id, token_hash=hashlib.sha256(token.encode()).hexdigest(),
            expires_at=(datetime.now(UTC) + timedelta(days=SESSION_DAYS)).isoformat(),
            created_at=utcnow_str(),
        ))
    response.set_cookie(COOKIE, token, httponly=True, secure=request.url.scheme == "https", samesite="lax", max_age=SESSION_DAYS * 86400, path="/")
    response.delete_cookie(GUEST_COOKIE, path="/")
    return {"user": _user_dict(user), "workspace_id": workspace.id}


@router.post("/signup")
def signup(body: SignupRequest, request: Request, response: Response) -> dict[str, Any]:
    email = str(body.email).strip().lower()
    with db_store.SessionLocal.begin() as db:
        if db.scalar(select(UserRow.id).limit(1)) is None:
            raise HTTPException(403, "administrator setup must be completed before account signup")
        if db.scalar(select(UserRow.id).where(UserRow.email == email)) is not None:
            raise HTTPException(409, "an account already exists for this email; sign in instead")
        user = UserRow(
            id=new_id(), email=email, display_name=body.name.strip(),
            password_hash=_password_hash(body.password), role="member", created_at=utcnow_str(),
            settings={"theme": "system", "instructions": ""},
        )
        workspace = WorkspaceRow(
            id=new_id(), name=f"{user.display_name}'s workspace", owner_id=user.id,
            kind="personal", settings={}, created_at=utcnow_str(),
        )
        db.add_all([user, workspace])
        db.flush()
        db.add(WorkspaceMemberRow(
            id=new_id(), workspace_id=workspace.id, user_id=user.id, role="owner", created_at=utcnow_str()
        ))
        _transfer_request_guest(db, request, user.id, workspace.id)
        token = secrets.token_urlsafe(32)
        db.add(LoginSessionRow(
            id=new_id(), user_id=user.id, token_hash=hashlib.sha256(token.encode()).hexdigest(),
            expires_at=(datetime.now(UTC) + timedelta(days=SESSION_DAYS)).isoformat(), created_at=utcnow_str(),
        ))
        user_data = _user_dict(user)
        workspace_id = workspace.id
    response.set_cookie(COOKIE, token, httponly=True, secure=request.url.scheme == "https",
                        samesite="lax", max_age=SESSION_DAYS * 86400, path="/")
    response.set_cookie("assayer_workspace", workspace_id, httponly=True, secure=request.url.scheme == "https",
                        samesite="lax", max_age=SESSION_DAYS * 86400, path="/")
    response.delete_cookie(GUEST_COOKIE, path="/")
    return {"user": user_data, "workspace_id": workspace_id}


@router.post("/login")
def login(body: Credentials, request: Request, response: Response) -> dict[str, Any]:
    with db_store.SessionLocal.begin() as db:
        user = db.scalar(select(UserRow).where(UserRow.email == str(body.email).lower()))
        if user is None or not _verify_password(body.password, user.password_hash):
            raise HTTPException(401, "email or password is incorrect")
        workspace = db.scalar(select(WorkspaceRow).join(
            WorkspaceMemberRow, WorkspaceMemberRow.workspace_id == WorkspaceRow.id
        ).where(WorkspaceMemberRow.user_id == user.id, WorkspaceRow.kind == "personal"))
        if workspace:
            _transfer_request_guest(db, request, user.id, workspace.id)
        token = secrets.token_urlsafe(32)
        db.add(LoginSessionRow(
            id=new_id(), user_id=user.id, token_hash=hashlib.sha256(token.encode()).hexdigest(),
            expires_at=(datetime.now(UTC) + timedelta(days=SESSION_DAYS)).isoformat(), created_at=utcnow_str(),
        ))
        user_data = _user_dict(user)
    response.set_cookie(COOKIE, token, httponly=True, secure=request.url.scheme == "https", samesite="lax", max_age=SESSION_DAYS * 86400, path="/")
    response.delete_cookie(GUEST_COOKIE, path="/")
    return {"user": user_data}


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response) -> None:
    token = request.cookies.get(COOKIE)
    if token:
        with db_store.SessionLocal.begin() as db:
            db.query(LoginSessionRow).filter_by(token_hash=hashlib.sha256(token.encode()).hexdigest()).delete()
    response.delete_cookie(COOKIE, path="/")


@router.get("/me")
def me(request: Request, user: UserRow = Depends(current_user)) -> dict[str, Any]:
    with db_store.SessionLocal() as db:
        memberships = db.scalars(select(WorkspaceMemberRow).where(WorkspaceMemberRow.user_id == user.id)).all()
        ids = [m.workspace_id for m in memberships]
        workspaces = db.scalars(select(WorkspaceRow).where(WorkspaceRow.id.in_(ids))).all() if ids else []
    active_id = request.cookies.get("assayer_workspace")
    return {"user": _user_dict(user), "workspaces": [
        {"id": w.id, "name": w.name, "kind": w.kind,
         "role": "active" if active_id == w.id else next((m.role for m in memberships if m.workspace_id == w.id), "member")}
        for w in workspaces
    ]}


@router.patch("/me")
def update_me(body: ProfilePatch, user: UserRow = Depends(current_user)) -> dict[str, Any]:
    with db_store.SessionLocal.begin() as db:
        row = db.get(UserRow, user.id)
        if row is None:
            raise HTTPException(404, "account not found")
        if body.name is not None:
            row.display_name = body.name.strip()
        if body.settings is not None:
            # Personal preferences only; unknown keys are not silently persisted.
            allowed = {"theme", "instructions", "font_size", "reduced_motion"}
            row.settings = {**(row.settings or {}), **{k: v for k, v in body.settings.items() if k in allowed}}
        result = _user_dict(row)
    return {"user": result}


@router.post("/change-password")
def change_password(body: PasswordChange, request: Request, user: UserRow = Depends(current_user)) -> dict[str, str]:
    with db_store.SessionLocal.begin() as db:
        row = db.get(UserRow, user.id)
        if row is None or not _verify_password(body.current_password, row.password_hash):
            raise HTTPException(400, "current password is incorrect")
        row.password_hash = _password_hash(body.new_password)
        current_token_hash = hashlib.sha256(request.cookies[COOKIE].encode()).hexdigest()
        db.query(LoginSessionRow).filter(
            LoginSessionRow.user_id == user.id,
            LoginSessionRow.token_hash != current_token_hash,
        ).delete(synchronize_session=False)
    return {"status": "password updated; other sessions signed out"}


@router.get("/workspaces")
def workspaces(user: UserRow = Depends(current_user)) -> dict[str, Any]:
    with db_store.SessionLocal() as db:
        memberships = db.scalars(select(WorkspaceMemberRow).where(WorkspaceMemberRow.user_id == user.id)).all()
        ids = [m.workspace_id for m in memberships]
        rows = db.scalars(select(WorkspaceRow).where(WorkspaceRow.id.in_(ids))).all() if ids else []
    return {"workspaces": [{"id": w.id, "name": w.name, "kind": w.kind,
                              "role": next((m.role for m in memberships if m.workspace_id == w.id), "member")}
                             for w in rows]}


@router.post("/workspaces")
def create_workspace(body: WorkspaceRequest, user: UserRow = Depends(current_user)) -> dict[str, Any]:
    workspace = WorkspaceRow(id=new_id(), name=body.name.strip(), owner_id=user.id,
                             kind="team", settings={}, created_at=utcnow_str())
    with db_store.SessionLocal.begin() as db:
        db.add(workspace)
        db.add(WorkspaceMemberRow(id=new_id(), workspace_id=workspace.id, user_id=user.id,
                                  role="owner", created_at=utcnow_str()))
    return {"id": workspace.id, "name": workspace.name, "kind": workspace.kind, "role": "owner"}


@router.post("/workspaces/activate")
def activate_workspace(body: ActivateWorkspaceRequest, request: Request, response: Response, user: UserRow = Depends(current_user)) -> dict[str, str]:
    with db_store.SessionLocal() as db:
        membership = db.scalar(select(WorkspaceMemberRow).where(
            WorkspaceMemberRow.workspace_id == body.workspace_id,
            WorkspaceMemberRow.user_id == user.id,
        ))
    if membership is None:
        raise HTTPException(404, "workspace not found")
    response.set_cookie("assayer_workspace", body.workspace_id, httponly=True,
                        secure=request.url.scheme == "https", samesite="lax",
                        max_age=SESSION_DAYS * 86400, path="/")
    return {"workspace_id": body.workspace_id}


@router.post("/workspaces/{workspace_id}/invites")
def invite_member(workspace_id: str, body: InviteRequest, user: UserRow = Depends(current_user)) -> dict[str, str]:
    email = body.email.lower().strip()
    token = secrets.token_urlsafe(36)
    with db_store.SessionLocal.begin() as db:
        workspace = db.get(WorkspaceRow, workspace_id)
        membership = db.scalar(select(WorkspaceMemberRow).where(
            WorkspaceMemberRow.workspace_id == workspace_id,
            WorkspaceMemberRow.user_id == user.id,
        ))
        if workspace is None or workspace.kind != "team" or membership is None or membership.role not in {"owner", "admin"}:
            raise HTTPException(404, "workspace not found")
        db.add(WorkspaceInviteRow(
            id=new_id(), email=email, workspace_id=workspace_id, role=body.role,
            token_hash=hashlib.sha256(token.encode()).hexdigest(), created_by=user.id,
            expires_at=(datetime.now(UTC) + timedelta(days=7)).isoformat(), created_at=utcnow_str(),
        ))
    return {"invite_url": f"{settings.web_origin.rstrip('/')}/?invite={token}"}


@router.post("/accept-invite")
def accept_invite(body: AcceptInviteRequest, request: Request, response: Response) -> dict[str, Any]:
    digest = hashlib.sha256(body.token.encode()).hexdigest()
    with db_store.SessionLocal.begin() as db:
        invite = db.scalar(select(WorkspaceInviteRow).where(WorkspaceInviteRow.token_hash == digest))
        if invite is None or invite.accepted_at is not None or invite.expires_at <= utcnow_str():
            raise HTTPException(400, "invite is invalid or expired")
        if body.email.strip().lower() != invite.email:
            raise HTTPException(403, "this invitation is for a different email address")
        if db.scalar(select(UserRow.id).where(UserRow.email == invite.email)) is not None:
            raise HTTPException(409, "an account already exists for this email; ask an administrator to add it to the workspace")
        user = UserRow(
            id=new_id(), email=invite.email, display_name=body.name.strip(),
            password_hash=_password_hash(body.password), role="member", created_at=utcnow_str(),
            settings={"theme": "system", "instructions": ""},
        )
        db.add(user)
        db.add(WorkspaceMemberRow(
            id=new_id(), workspace_id=invite.workspace_id, user_id=user.id,
            role=invite.role, created_at=utcnow_str(),
        ))
        invite.accepted_at = utcnow_str()
        _transfer_request_guest(db, request, user.id, invite.workspace_id)
        token = secrets.token_urlsafe(32)
        db.add(LoginSessionRow(
            id=new_id(), user_id=user.id, token_hash=hashlib.sha256(token.encode()).hexdigest(),
            expires_at=(datetime.now(UTC) + timedelta(days=SESSION_DAYS)).isoformat(), created_at=utcnow_str(),
        ))
        workspace_id = invite.workspace_id
    response.set_cookie(COOKIE, token, httponly=True, secure=request.url.scheme == "https",
                        samesite="lax", max_age=SESSION_DAYS * 86400, path="/")
    response.set_cookie("assayer_workspace", workspace_id, httponly=True, secure=request.url.scheme == "https",
                        samesite="lax", max_age=SESSION_DAYS * 86400, path="/")
    return {"user": _user_dict(user), "workspace_id": workspace_id}


@router.get("/invite/{token}")
def get_invite(token: str) -> dict[str, str]:
    digest = hashlib.sha256(token.encode()).hexdigest()
    with db_store.SessionLocal() as db:
        invite = db.scalar(select(WorkspaceInviteRow).where(WorkspaceInviteRow.token_hash == digest))
        if invite is None or invite.accepted_at is not None or invite.expires_at <= utcnow_str():
            raise HTTPException(404, "invite is invalid or expired")
        workspace = db.get(WorkspaceRow, invite.workspace_id)
        return {"email": invite.email, "workspace": workspace.name if workspace else "team workspace"}


@router.post("/join-invite")
def join_invite(body: JoinInviteRequest, request: Request, response: Response,
                user: UserRow = Depends(current_user)) -> dict[str, str]:
    digest = hashlib.sha256(body.token.encode()).hexdigest()
    with db_store.SessionLocal.begin() as db:
        invite = db.scalar(select(WorkspaceInviteRow).where(WorkspaceInviteRow.token_hash == digest))
        if invite is None or invite.accepted_at is not None or invite.expires_at <= utcnow_str():
            raise HTTPException(400, "invite is invalid or expired")
        if invite.email != user.email:
            raise HTTPException(403, "this invitation is for a different email address")
        membership = db.scalar(select(WorkspaceMemberRow).where(
            WorkspaceMemberRow.workspace_id == invite.workspace_id,
            WorkspaceMemberRow.user_id == user.id,
        ))
        if membership is None:
            db.add(WorkspaceMemberRow(
                id=new_id(), workspace_id=invite.workspace_id, user_id=user.id,
                role=invite.role, created_at=utcnow_str(),
            ))
        invite.accepted_at = utcnow_str()
        workspace_id = invite.workspace_id
    response.set_cookie("assayer_workspace", workspace_id, httponly=True,
                        secure=request.url.scheme == "https", samesite="lax",
                        max_age=SESSION_DAYS * 86400, path="/")
    return {"workspace_id": workspace_id}


@router.get("/admin/settings")
def get_instance_settings(user: UserRow = Depends(current_user)) -> dict[str, Any]:
    if user.role != "admin":
        raise HTTPException(403, "administrator role required")
    defaults = {
        "run_max_checks": settings.run_max_checks,
        "run_max_concurrency": settings.run_max_concurrency,
        "run_max_row_sample": settings.run_max_row_sample,
        "ui_visual_default_tolerance": settings.ui_visual_default_tolerance,
    }
    with db_store.SessionLocal() as db:
        row = db.get(AppSettingsRow, "instance")
    return {"settings": {**defaults, **(row.values if row else {})}}


@router.patch("/admin/settings")
def update_instance_settings(body: InstanceSettingsPatch, user: UserRow = Depends(current_user)) -> dict[str, Any]:
    if user.role != "admin":
        raise HTTPException(403, "administrator role required")
    patch = body.model_dump(exclude_none=True)
    with db_store.SessionLocal.begin() as db:
        row = db.get(AppSettingsRow, "instance")
        if row is None:
            row = AppSettingsRow(id="instance", values={}, updated_at=utcnow_str())
            db.add(row)
        row.values = {**(row.values or {}), **patch}
        row.updated_at = utcnow_str()
        result = dict(row.values)
    return {"settings": result}
