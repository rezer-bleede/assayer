"""End-to-end intake through the upload route: routing, profiles and trust mode.

These call the route coroutine directly with the same arguments FastAPI would
bind, so no HTTP server (and no multipart encoder) is needed to prove that the
routing, profiling and filtering behave as the UI expects.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import pytest
from fastapi import HTTPException

from app.routers import etl as etl_router


@dataclass
class _Upload:
    """Stand-in for fastapi.UploadFile: the route only needs read()/filename."""

    filename: str
    payload: bytes

    async def read(self) -> bytes:
        return self.payload


def _call(files: list[tuple[str, bytes]], *, mode: str = "folder", paths: str | None = None):
    uploads = [
        _Upload(filename=rel.rsplit("/", 1)[-1], payload=body) for rel, body in files
    ]
    return asyncio.run(
        etl_router.upload(
            session_id=None,
            mode=mode,
            files=uploads,  # type: ignore[arg-type]
            paths=paths if paths is not None else json.dumps([rel for rel, _ in files]),
        )
    )


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    """The route persists analyses; give it a throwaway SQLite file."""
    import contextlib

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.store.db as db_mod
    import app.store.repository as repo_mod
    from app.store.db import Base

    engine = create_engine(
        f"sqlite:///{tmp_path / 'state.db'}", connect_args={"check_same_thread": False}
    )
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
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


def _project() -> list[tuple[str, bytes]]:
    return [
        ("shop/dbt_project.yml", b"name: shop\n"),
        ("shop/models/stg_orders.sql", b"select * from {{ source('raw','orders') }}"),
        ("shop/models/orders.sql", b"select * from {{ ref('stg_orders') }}"),
        (
            "shop/seeds/raw_customers.csv",
            b"id,email\n1,a@b.co\n2,c@d.co\n",
        ),
        ("shop/data/export.json", b'[{"id": 1, "amount": 2}]'),
        ("shop/package-lock.json", b'{"lockfileVersion": 3, "packages": {}}'),
        ("shop/node_modules/lib/index.js", b"module.exports = 1"),
        ("shop/.git/config", b"[core]"),
        ("shop/notes.txt", b"shopping list"),
    ]


def test_folder_upload_routes_and_profiles() -> None:
    res = _call(_project())
    analysis = res.analysis

    assert analysis.label == "shop"
    assert "shop/seeds/raw_customers.csv" in analysis.files  # seed registered by name
    assert "shop/package-lock.json" not in analysis.files
    assert "shop/notes.txt" not in analysis.files
    assert "shop/node_modules/lib/index.js" not in analysis.files

    tables = {p.table for p in analysis.data_profiles}
    assert tables == {"raw_customers", "export"}
    seed = next(p for p in analysis.data_profiles if p.table == "raw_customers")
    assert seed.row_count == 2
    assert seed.column_names() == ["id", "email"]

    assert any("Ignored" in r for r in analysis.risks)


def test_upload_returns_required_sources_for_the_parsed_project() -> None:
    res = _call(_project())
    assert res.analysis.language.value == "dbt"
    keys = {r.key for r in res.required_sources}
    assert keys  # the SQL references a source that lives in a database


def test_trust_mode_keeps_what_the_user_named() -> None:
    files = [("models/orders.sql", b"select * from dump"), ("dump.json", b'[{"id": 1}]')]

    strict = _call(files)
    assert [p.table for p in strict.analysis.data_profiles] == ["dump"]
    assert strict.analysis.data_profiles[0].row_count == 1
    assert "dump.json" not in strict.analysis.files

    trusted = _call(files, mode="files")
    assert trusted.analysis.data_profiles == []
    assert "dump.json" in trusted.analysis.files


def test_data_files_alone_are_reported_not_guessed() -> None:
    with pytest.raises(HTTPException) as exc:
        _call([("data/customers.csv", b"id,email\n1,a@b.co\n")])
    assert "Profiled 1 data file(s)" in exc.value.detail
    assert "customers.csv" in exc.value.detail


def test_upload_rejects_a_pile_of_noise_with_a_useful_message() -> None:
    with pytest.raises(HTTPException) as exc:
        _call([("a/node_modules/x/index.js", b"x"), ("b/.git/config", b"y")])
    detail = exc.value.detail
    assert "no readable ETL source" in detail
    assert ".sql" in detail


def test_upload_rejects_an_empty_request() -> None:
    with pytest.raises(HTTPException) as exc:
        _call([])
    assert exc.value.status_code == 400