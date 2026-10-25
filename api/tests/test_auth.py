"""Bootstrap and cookie-session authentication behavior."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def auth_client(tmp_path, monkeypatch):
    from app.core.config import settings
    from app.store import db
    from app.store.db import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'auth.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)
    monkeypatch.setattr(settings, "bootstrap_token", "test-bootstrap-token-123456")
    from app.main import app

    monkeypatch.setattr(app.state, "auth_ready", True)
    yield TestClient(app)
    engine.dispose()


def test_bootstrap_creates_admin_and_closes_setup(auth_client) -> None:
    client = auth_client
    status = client.get("/api/auth/status")
    assert status.json()["setup_required"] is True

    created = client.post("/api/auth/bootstrap", json={
        "name": "Admin", "email": "admin@example.com", "password": "long-secure-password-123",
        "setup_token": "test-bootstrap-token-123456",
    })
    assert created.status_code == 200
    assert created.json()["user"]["role"] == "admin"
    assert "assayer_session" in created.cookies
    assert client.get("/api/auth/status").json()["setup_required"] is False

    duplicate = client.post("/api/auth/bootstrap", json={
        "name": "Another", "email": "another@example.com", "password": "long-secure-password-456",
        "setup_token": "test-bootstrap-token-123456",
    })
    assert duplicate.status_code == 409


def test_password_login_and_logout(auth_client) -> None:
    client = auth_client
    client.post("/api/auth/bootstrap", json={
        "name": "Admin", "email": "admin@example.com", "password": "long-secure-password-123",
        "setup_token": "test-bootstrap-token-123456",
    })
    client.post("/api/auth/logout")
    failed = client.post("/api/auth/login", json={"email": "admin@example.com", "password": "incorrect-password"})
    assert failed.status_code == 401
    success = client.post("/api/auth/login", json={"email": "admin@example.com", "password": "long-secure-password-123"})
    assert success.status_code == 200
    assert client.get("/api/auth/me").json()["user"]["email"] == "admin@example.com"
    changed = client.post("/api/auth/change-password", json={
        "current_password": "long-secure-password-123", "new_password": "replacement-password-789",
    })
    assert changed.status_code == 200
    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/threads/get").status_code == 401
    assert client.post("/api/auth/login", json={"email": "admin@example.com", "password": "long-secure-password-123"}).status_code == 401
    assert client.post("/api/auth/login", json={"email": "admin@example.com", "password": "replacement-password-789"}).status_code == 200


def test_team_invite_is_one_time_and_creates_member(auth_client) -> None:
    owner = auth_client
    owner.post("/api/auth/bootstrap", json={
        "name": "Admin", "email": "admin@example.com", "password": "long-secure-password-123",
        "setup_token": "test-bootstrap-token-123456",
    })
    workspace = owner.post("/api/auth/workspaces", json={"name": "QA team"}).json()
    invite = owner.post(f"/api/auth/workspaces/{workspace['id']}/invites", json={"email": "teammate@example.com"})
    assert invite.status_code == 200
    token = parse_qs(urlparse(invite.json()["invite_url"]).query)["invite"][0]

    from fastapi.testclient import TestClient

    from app.main import app

    teammate = TestClient(app)
    assert teammate.get(f"/api/auth/invite/{token}").json()["email"] == "teammate@example.com"
    accepted = teammate.post("/api/auth/accept-invite", json={
        "token": token, "name": "Teammate", "email": "teammate@example.com",
        "password": "another-secure-password-456",
    })
    assert accepted.status_code == 200
    assert accepted.json()["workspace_id"] == workspace["id"]
    assert teammate.post("/api/auth/accept-invite", json={
        "token": token, "name": "Teammate", "email": "teammate@example.com",
        "password": "another-secure-password-456",
    }).status_code == 400
