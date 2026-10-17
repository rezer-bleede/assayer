"""Create tables and exit. Used by `make migrate` and on first boot."""

from __future__ import annotations

from app.store.db import init_db

if __name__ == "__main__":
    init_db()
    print("migrations applied")