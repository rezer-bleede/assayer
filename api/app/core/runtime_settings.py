"""Persistent, administrator-managed settings shared with API and workers."""

import time
from threading import Lock
from typing import Any

_cache: dict[str, Any] = {}
_loaded_at = 0.0
_lock = Lock()


def _load_values() -> dict[str, Any]:
    global _cache, _loaded_at
    with _lock:
        if time.monotonic() - _loaded_at < 2:
            return _cache
        try:
            from app.store.db import AppSettingsRow, SessionLocal

            with SessionLocal() as db:
                row = db.get(AppSettingsRow, "instance")
                _cache = dict(row.values or {}) if row else {}
        except Exception:  # env config remains the fallback if storage is unavailable
            _cache = {}
        _loaded_at = time.monotonic()
        return _cache


def get_runtime_setting(name: str, default: Any) -> Any:
    return _load_values().get(name, default)
