"""Execution of approved browser-based checks."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.util
import math
import re
import shutil
import time
from pathlib import Path
from typing import Any

from app.connectors.browser import BrowserClient, _safe_url
from app.connectors.factory import build_connector
from app.core.config import settings
from app.runner.compiler import assert_readonly
from app.schemas import CheckResult, CheckSpec, CheckStatus, ConnectionSpec

try:
    import regex as bounded_regex
except ImportError:  # pragma: no cover - UI extra installs the bounded matcher
    bounded_regex = None


def _artifact(run_id: str, check_id: str, name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,128}", check_id
    ):
        raise ValueError("invalid run or check identifier for artifact path")
    if name not in {"screenshot.png", "console.txt", "network.txt", "current.png", "baseline.png", "diff.png"}:
        raise ValueError("invalid artifact name")
    root = Path(settings.ui_artifact_dir).resolve()
    folder = (root / run_id / check_id).resolve()
    if root not in folder.parents:
        raise ValueError("artifact path escapes artifact directory")
    folder.mkdir(parents=True, exist_ok=True)
    path = (folder / name).resolve()
    if folder not in path.parents:
        raise ValueError("artifact path escapes check directory")
    return str(path.relative_to(root))


def _capture(client: BrowserClient, check: CheckSpec, run_id: str, page: Any) -> list[str]:
    paths = []
    root = Path(settings.ui_artifact_dir).resolve()
    shot = _artifact(run_id, check.id, "screenshot.png")
    page.screenshot(path=str(root / shot), full_page=True)
    paths.append(shot)
    console, network = client.events(check.url)
    for filename, value in (("console.txt", console), ("network.txt", network)):
        rel = _artifact(run_id, check.id, filename)
        (root / rel).write_text("\n".join(value), encoding="utf-8")
        paths.append(rel)
    return paths


def _display_number(text: str) -> tuple[float, bool]:
    match = re.search(r"-?\d[\d,]*(?:\.\d+)?\s*[kKmMbB]?\s*%?", text)
    if not match:
        raise ValueError("UI value does not contain a number")
    token = match.group().replace(",", "").replace(" ", "")
    percent = token.endswith("%")
    if percent:
        token = token[:-1]
    multiplier = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get(token[-1:].lower(), 1)
    if multiplier != 1:
        token = token[:-1]
    return float(token) * multiplier, percent


def _ignored_events(events: list[str], patterns: Any) -> list[str]:
    if not isinstance(patterns, list):
        return events
    compiled = []
    for pattern in patterns[:30]:
        try:
            matcher = bounded_regex if bounded_regex is not None else re
            compiled.append(matcher.compile(str(pattern)[:200], re.I))
        except Exception:
            continue
    remaining = []
    for event in events:
        ignored = False
        for pattern in compiled:
            try:
                if bounded_regex is not None:
                    ignored = pattern.search(event, timeout=0.02) is not None
                else:
                    ignored = pattern.search(event) is not None
            except TimeoutError:
                ignored = False
            if ignored:
                break
        if not ignored:
            remaining.append(event)
    return remaining


def _run_one(client: BrowserClient, check: CheckSpec, connections: dict[str, ConnectionSpec], run_id: str) -> CheckResult:
    page = None
    result = CheckResult(
        check_id=check.id, name=check.name, kind=check.kind, table=check.table,
        column=check.column, severity=check.severity, url=check.url,
    )
    start = time.perf_counter()
    try:
        page = client.page(check.url)
        viewport = check.params.get("viewport")
        width = int(viewport.get("width", 1280)) if isinstance(viewport, dict) else 1280
        height = int(viewport.get("height", 720)) if isinstance(viewport, dict) else 720
        if not (320 <= width <= 3840 and 240 <= height <= 2160):
            raise ValueError("viewport dimensions must be within 320x240 and 3840x2160")
        page.set_viewport_size({"width": width, "height": height})
        kind = str(check.kind)
        if kind == "page_load":
            result.observed = page.title()
            result.expected = "HTTP status below 400"
            code = client.status_code(check.url)
            result.observed = f"{code} {page.title()}" if code else page.title()
            result.status = CheckStatus.FAILED if code and code >= 400 else CheckStatus.PASSED
            result.message = f"page loaded: {_safe_url(page.url)} (HTTP {code or 'unknown'})"
            title_pattern = check.params.get("title_pattern") or check.expected
            if title_pattern:
                if len(str(title_pattern)) > 500:
                    raise ValueError("title pattern exceeds the 500-character limit")
                matcher = bounded_regex if bounded_regex is not None else re
                try:
                    title_matches = (
                        matcher.search(str(title_pattern), page.title(), timeout=0.05)
                        if bounded_regex is not None
                        else matcher.search(str(title_pattern), page.title())
                    )
                except TimeoutError as exc:
                    raise ValueError("title pattern exceeded its matching time limit") from exc
                title_matches = bool(title_matches)
            else:
                title_matches = True
            if not title_matches:
                result.status = CheckStatus.FAILED
                result.expected = str(title_pattern)
                result.message = f"page title did not match {title_pattern!r}"
        elif kind == "element_present":
            locator = page.locator(check.selector or "")
            ok = locator.count() > 0 and locator.first.is_visible()
            result.status = CheckStatus.PASSED if ok else CheckStatus.FAILED
            result.observed, result.expected = str(ok), "visible element"
            result.message = f"selector {check.selector!r} {'is' if ok else 'is not'} visible"
        elif kind == "element_text":
            if not check.expected:
                raise ValueError("element_text checks require expected text")
            locator = page.locator(check.selector or "")
            text = "" if locator.count() == 0 else locator.first.inner_text().strip()
            result.observed, result.expected = text[:500] if text else "element not found", check.expected
            ok = bool(text) and check.expected in text
            result.status = CheckStatus.PASSED if ok else CheckStatus.FAILED
            result.message = "expected text found" if ok else f"expected text {check.expected!r} not found"
        elif kind in {"console_clean", "network_clean"}:
            settle_ms = max(0, min(int(check.params.get("settle_ms", 300)), 2000))
            if settle_ms:
                page.wait_for_timeout(settle_ms)
            console, network = client.events(check.url)
            errors = _ignored_events(console if kind == "console_clean" else network, check.params.get("ignore_patterns"))
            result.observed, result.expected = len(errors), 0
            result.status = CheckStatus.FAILED if errors else CheckStatus.PASSED
            result.message = f"{len(errors)} {'console error(s)' if kind == 'console_clean' else 'failed request(s)' }"
        elif kind == "ui_parity":
            query = str(check.params.get("query") or "")
            assert_readonly(query)
            source_id = str(check.params.get("source_id") or "")
            conn = build_connector(connections[source_id])
            try:
                query_result = conn.query(query)
                rows = query_result.rows
            finally:
                conn.dispose()
            if len(rows) != 1 or len(rows[0]) != 1:
                raise ValueError("UI parity query must return exactly one row and one column")
            expected = float(next(iter(rows[0].values())))
            value_locator = page.locator(check.selector or "")
            if value_locator.count() == 0:
                result.status = CheckStatus.FAILED
                result.message = f"UI selector {check.selector!r} was not found"
                result.expected = expected
                result.observed = "element not found"
                result.duration_ms = round((time.perf_counter() - start) * 1000, 2)
                return result
            text = value_locator.first.inner_text().strip()
            observed, is_percent = _display_number(text)
            if is_percent and abs(expected) <= 1:
                observed /= 100
            tolerance = float(check.params.get("tolerance", 0))
            if not math.isfinite(tolerance) or tolerance < 0:
                raise ValueError("UI parity tolerance must be a finite nonnegative number")
            ok = abs(observed - expected) <= max(abs(expected) * tolerance, tolerance)
            result.observed, result.expected = observed, expected
            result.status = CheckStatus.PASSED if ok else CheckStatus.FAILED
            result.message = f"UI value {observed:g}; warehouse value {expected:g} (tolerance {tolerance:g})"
        elif kind == "flow":
            if len(check.steps) > settings.ui_max_steps:
                raise ValueError(f"browser flow exceeds the {settings.ui_max_steps}-step limit")
            deadline = start + settings.ui_check_timeout_seconds
            for step in check.steps:
                remaining_ms = int((deadline - time.perf_counter()) * 1000)
                if remaining_ms <= 0:
                    raise TimeoutError("browser flow exceeded its check timeout")
                page.set_default_timeout(min(settings.ui_check_timeout_seconds * 1000, remaining_ms))
                page.set_default_navigation_timeout(min(settings.ui_nav_timeout_seconds * 1000, remaining_ms))
                selector = step.selector or ""
                if step.action == "goto":
                    page.goto(client.navigation_url(step.value))
                elif step.action == "click":
                    page.locator(selector).click()
                elif step.action == "fill":
                    page.locator(selector).fill(step.value or "")
                elif step.action == "press":
                    page.locator(selector).press(step.value or "Enter")
                elif step.action == "wait":
                    page.wait_for_timeout(min(int(step.value or "500"), 5000))
                elif step.action == "assert_present":
                    assert page.locator(selector).count() > 0, f"missing {selector}"
                elif step.action == "assert_text":
                    assert (step.value or "") in page.locator(selector).first.inner_text(), f"text missing from {selector}"
            result.status, result.message = CheckStatus.PASSED, f"completed {len(check.steps)} flow step(s)"
        elif kind == "visual_diff":
            import numpy as np
            from PIL import Image, ImageChops

            root = Path(settings.ui_artifact_dir).resolve()
            current_rel = _artifact(run_id, check.id, "current.png")
            current_path = root / current_rel
            page.screenshot(path=str(current_path), full_page=True)
            viewport_key = f"{width}x{height}"
            route_hash = hashlib.sha256(f"{check.url or '/'}|{viewport_key}".encode()).hexdigest()[:16]
            source_hash = hashlib.sha256((check.source_id or "web").encode()).hexdigest()[:16]
            baseline_path = root / "baselines" / f"{source_hash}-{route_hash}.png"
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            baseline_artifact = _artifact(run_id, check.id, "baseline.png")
            if not baseline_path.exists() or check.params.get("rebaseline") is True:
                baseline_path.write_bytes(current_path.read_bytes())
                result.status, result.message = CheckStatus.PASSED, (
                    "visual baseline updated" if check.params.get("rebaseline") else "visual baseline recorded"
                )
                result.observed, result.expected = 0, "baseline recorded"
            else:
                a, b = Image.open(current_path).convert("RGB"), Image.open(baseline_path).convert("RGB")
                if a.size != b.size:
                    ratio = 1.0
                    diff = Image.new("RGB", a.size, (255, 0, 0))
                else:
                    diff = ImageChops.difference(a, b)
                    ratio = float(np.asarray(diff).max(axis=2).astype(bool).mean())
                tolerance = float(check.params.get("tolerance", settings.ui_visual_default_tolerance))
                if not math.isfinite(tolerance) or tolerance < 0:
                    raise ValueError("visual tolerance must be a finite nonnegative number")
                result.observed, result.expected = round(ratio, 6), tolerance
                result.status = CheckStatus.PASSED if ratio <= tolerance else CheckStatus.FAILED
                result.message = f"visual difference {ratio:.3%}; tolerance {tolerance:.3%}"
                diff_rel = _artifact(run_id, check.id, "diff.png")
                diff.save(root / diff_rel)
                result.artifacts.append(diff_rel)
            shutil.copyfile(baseline_path, root / baseline_artifact)
            result.artifacts.append(current_rel)
            result.artifacts.append(baseline_artifact)
        else:
            raise ValueError(f"unsupported UI check kind: {kind}")
        if result.status == CheckStatus.FAILED:
            result.artifacts.extend(_capture(client, check, run_id, page))
    except Exception as exc:
        result.status = CheckStatus.ERROR
        result.message = f"{type(exc).__name__}: {exc}"[:400]
        with contextlib.suppress(Exception):
            result.artifacts.extend(_capture(client, check, run_id, page))
    result.duration_ms = round((time.perf_counter() - start) * 1000, 2)
    return result


def _execute_group(checks: list[CheckSpec], connection: ConnectionSpec, connections: dict[str, ConnectionSpec], run_id: str) -> list[CheckResult]:
    try:
        with BrowserClient(connection) as client:
            # Leave interactions until last so clicks/form submits don't alter
            # the page observed by smoke, parity, and screenshot checks.
            ordered = sorted(checks, key=lambda check: str(check.kind) == "flow")
            return [_run_one(client, check, connections, run_id) for check in ordered]
    except Exception as exc:
        return [CheckResult(check_id=c.id, name=c.name, kind=c.kind, table=c.table, url=c.url,
                            severity=c.severity, status=CheckStatus.ERROR, message=str(exc)[:400]) for c in checks]


async def execute_ui_checks(checks: list[CheckSpec], connections: dict[str, ConnectionSpec], run_id: str) -> list[CheckResult]:
    if not settings.ui_enabled or importlib.util.find_spec("playwright") is None:
        why = "UI checks are disabled" if not settings.ui_enabled else "Playwright is not installed"
        return [
            CheckResult(check_id=c.id, name=c.name, kind=c.kind, table=c.table, url=c.url,
                        severity=c.severity, status=CheckStatus.SKIPPED, message=why)
            for c in checks
        ]
    groups: dict[str, list[CheckSpec]] = {}
    for check in checks:
        groups.setdefault(check.source_id or "", []).append(check)
    results: list[CheckResult] = []
    for source_id, grouped in groups.items():
        spec = connections.get(source_id)
        if spec is None:
            results.extend(CheckResult(check_id=c.id, name=c.name, kind=c.kind, table=c.table, url=c.url,
                                       severity=c.severity, status=CheckStatus.ERROR, message="unknown web target") for c in grouped)
            continue
        results.extend(await asyncio.to_thread(_execute_group, grouped, spec, connections, run_id))
    return results
