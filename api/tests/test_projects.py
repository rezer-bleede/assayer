"""Projects: grouping chats, instructions + knowledge files, prompt injection."""

from __future__ import annotations

import contextlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.store import repository as repo


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """Point the repository at a throwaway SQLite file."""
    import app.store.db as db_mod
    import app.store.repository as repo_mod

    engine = create_engine(
        f"sqlite:///{tmp_path / 'state.db'}", connect_args={"check_same_thread": False}
    )
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    from app.store.db import Base

    Base.metadata.create_all(engine)

    @contextlib.contextmanager
    def session():
        s = factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", factory)
    monkeypatch.setattr(repo_mod, "write_session", session)
    yield engine
    engine.dispose()


@pytest.fixture()
def client(store):
    from app.main import app

    return TestClient(app)


# --------------------------------------------------------------------------- #
# repository layer
# --------------------------------------------------------------------------- #


def test_project_crud_and_filing(store) -> None:
    project = repo.save_project("Acme DWH", instructions="Use SNOMED codes.")
    assert repo.get_project(project["id"])["name"] == "Acme DWH"

    repo.ensure_session("t1")
    repo.update_session_payload("t1", {"title": "Audit orders"})
    repo.set_thread_project("t1", project["id"])
    assert repo.get_thread_project_id("t1") == project["id"]

    listed = repo.list_threads()
    assert next(t for t in listed if t["id"] == "t1")["projectId"] == project["id"]

    projects = repo.list_projects()
    assert projects[0]["threadCount"] == 1

    # unfile
    repo.set_thread_project("t1", None)
    assert repo.get_thread_project_id("t1") is None

    assert repo.update_project(project["id"], name="Acme DWH v2")["name"] == "Acme DWH v2"
    assert repo.delete_project(project["id"]) is True
    assert repo.get_project(project["id"]) is None


def test_project_delete_unfiles_chats_and_removes_files(store) -> None:
    project = repo.save_project("P")
    other = repo.save_project("Q")
    repo.ensure_session("t1")
    repo.set_thread_project("t1", project["id"])
    repo.add_project_file(project["id"], "rules.md", "nulls are blockers")

    assert repo.delete_project(project["id"]) is True
    assert repo.get_thread_project_id("t1") is None
    assert repo.list_project_files(project["id"]) == []
    assert repo.get_project(other["id"]) is not None  # untouched


def test_project_files_round_trip(store) -> None:
    project = repo.save_project("P")
    f = repo.add_project_file(project["id"], "metrics.md", "ARR = sum(acv)")
    files = repo.list_project_files(project["id"], with_content=True)
    assert files[0]["id"] == f["id"]
    assert files[0]["text"] == "ARR = sum(acv)"
    assert repo.delete_project_file(project["id"], f["id"]) is True
    assert repo.list_project_files(project["id"]) == []


# --------------------------------------------------------------------------- #
# context rendering / injection
# --------------------------------------------------------------------------- #


def test_render_project_context_blocks_and_budget(store) -> None:
    from app.services.projects import FILE_PROMPT_BUDGET, render_project_context

    block = render_project_context(
        "Acme",
        "Never test staging tables.",
        [
            {"name": "rules.md", "text": "a" * (FILE_PROMPT_BUDGET * 3)},
            {"name": "empty.md", "text": "   "},
        ],
    )
    assert block is not None
    assert "# Project: Acme" in block
    assert "## Instructions\nNever test staging tables." in block
    assert "## Knowledge file: rules.md" in block
    assert "…[truncated]" in block
    assert "empty.md" not in block


def test_render_project_context_empty_returns_none() -> None:
    from app.services.projects import render_project_context

    assert render_project_context("P", "", []) is None


def test_project_context_for_session(store) -> None:
    from app.services.projects import project_context_for_session

    assert project_context_for_session("t0") is None

    repo.ensure_session("t0")
    project = repo.save_project("Acme", instructions="Focus on revenue tables.")
    repo.add_project_file(project["id"], "k.md", "rev_net = gross - refunds")
    repo.set_thread_project("t0", project["id"])

    ctx = project_context_for_session("t0")
    assert ctx is not None
    assert "Focus on revenue tables." in ctx
    assert "rev_net = gross - refunds" in ctx


def test_plan_and_report_prompts_include_project_context() -> None:
    from app.agent.prompts import plan_prompt, report_prompt

    plan = plan_prompt("etl", [], "intent", project_context="# Project: P\n## Instructions\nX")
    assert plan.startswith("# Project: P")
    assert "X" in plan

    report = report_prompt({"a": 1}, [], project_context="# Project: P\n## Instructions\nY")
    assert "Y" in report


# --------------------------------------------------------------------------- #
# HTTP surface
# --------------------------------------------------------------------------- #


def test_projects_http_crud(client) -> None:
    created = client.post("/api/projects", json={"name": "Acme", "instructions": "i"})
    assert created.status_code == 200
    pid = created.json()["id"]

    assert client.get("/api/projects").json()["projects"][0]["id"] == pid
    assert client.patch(f"/api/projects/{pid}", json={"name": "Acme2"}).json()["name"] == "Acme2"
    assert client.get("/api/projects/nope").status_code == 404

    assert client.delete(f"/api/projects/{pid}").status_code == 204
    assert client.get(f"/api/projects/{pid}").status_code == 404


def test_thread_create_with_project_and_move(client) -> None:
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    other = client.post("/api/projects", json={"name": "Q"}).json()["id"]

    thread = client.post(
        "/api/threads/create",
        json={"messages": [{"role": "user", "content": "audit my dbt project"}], "projectId": pid},
    ).json()
    assert thread["projectId"] == pid

    listed = client.get("/api/threads/get").json()["threads"]
    assert next(t for t in listed if t["id"] == thread["id"])["projectId"] == pid

    moved = client.post("/api/threads/move", json={"threadId": thread["id"], "projectId": other})
    assert moved.status_code == 200 and moved.json()["projectId"] == other

    assert (
        client.post(
            "/api/threads/move", json={"threadId": thread["id"], "projectId": "nope"}
        ).status_code
        == 404
    )

    unfiled = client.post("/api/threads/move", json={"threadId": thread["id"], "projectId": None})
    assert unfiled.json()["projectId"] is None


def test_project_files_upload_and_rejections(client) -> None:
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]

    res = client.post(
        f"/api/projects/{pid}/files",
        files=[
            ("files", ("rules.md", "nulls are blockers", "text/markdown")),
            ("files", ("photo.png", b"\x89PNG\r\n\x1a\n\x00\x00", "image/png")),
        ],
    )
    body = res.json()
    assert res.status_code == 200
    assert [f["name"] for f in body["files"]] == ["rules.md"]
    assert body["rejected"][0]["name"] == "photo.png"

    files = client.get(f"/api/projects/{pid}/files").json()["files"]
    assert len(files) == 1
    assert client.delete(f"/api/projects/{pid}/files/{files[0]['id']}").status_code == 204
    assert client.get(f"/api/projects/{pid}/files").json()["files"] == []

    assert client.get("/api/projects/nope/files").status_code == 404
