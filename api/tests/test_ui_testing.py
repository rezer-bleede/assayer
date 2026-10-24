from __future__ import annotations

import asyncio
import json
import sqlite3
from asyncio import run
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

import app.agent.plan_builder as builder
from app.agent.plan_builder import _coerce_ui, _merge, _requests_rebaseline, build_plan
from app.connectors.browser import BrowserClient, BrowserError
from app.runner.ui_checks import _artifact, _display_number, _ignored_events, execute_ui_checks
from app.schemas import CheckKind, CheckSpec, ConnectionSpec, Severity, SourceType
from app.services.ingest import extract_connections


def _require_chromium() -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        browser.close()


def test_http_url_is_ingested_as_web_target() -> None:
    specs = extract_connections("Please test the dashboard at https://example.test/revenue")
    assert len(specs) == 1
    assert specs[0].type == SourceType.WEB
    assert specs[0].url == "https://example.test/revenue"


def test_web_basic_auth_is_not_left_in_the_target_url() -> None:
    spec = extract_connections("https://alice:s3cret@example.test/app?token=private")[0]
    assert spec.url == "https://example.test/app"
    assert spec.auth_mode == "basic"
    assert spec.auth_username == "alice"
    assert "s3cret" not in str(spec.redacted())


def test_web_specs_reject_credentials_embedded_in_url() -> None:
    with pytest.raises(ValueError, match="web URL credentials"):
        ConnectionSpec(type=SourceType.WEB, name="app", url="https://user:secret@example.test")


def test_web_query_values_are_redacted_from_connection_responses() -> None:
    spec = ConnectionSpec(type=SourceType.WEB, name="app", url="https://example.test/app?token=secret&view=table")
    redacted = str(spec.redacted())
    assert "secret" not in redacted
    assert "view=table" not in redacted


def test_web_plan_includes_smoke_visual_and_flow_checks(monkeypatch) -> None:
    from asyncio import run

    class FakeBrowser:
        def __init__(self, _spec):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def discover_routes(self):
            return [{"path": "/", "title": "Fixture"}, {"path": "/dashboard", "title": "Dashboard"}]

    monkeypatch.setattr("app.connectors.browser.BrowserClient", FakeBrowser)
    spec = ConnectionSpec(type=SourceType.WEB, name="dashboard", url="https://example.test")
    plan = run(build_plan(None, [], use_llm=False, connections={"web1": spec}))
    kinds = {c.kind for c in plan.checks}
    assert {CheckKind.PAGE_LOAD, CheckKind.CONSOLE_CLEAN, CheckKind.NETWORK_CLEAN,
            CheckKind.ELEMENT_PRESENT, CheckKind.VISUAL_DIFF, CheckKind.FLOW} <= kinds
    assert all(c.table is None and c.source_id == "web1" for c in plan.checks)
    assert any(c.url == "/dashboard" for c in plan.checks)


def test_web_only_plans_allow_llm_authored_ui_assertions(monkeypatch) -> None:
    monkeypatch.setattr(builder, "llm_available", lambda: True)

    class FakeBrowser:
        def __init__(self, _spec): pass
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def discover_routes(self):
            return [{"path": "/", "title": "Home", "selectors": ["body", "main"]},
                    {"path": "/dashboard", "title": "Dashboard", "selectors": ["body", "main", "h1"]}]

    monkeypatch.setattr("app.connectors.browser.BrowserClient", FakeBrowser)

    async def fake_ask(_prompt, on_thought=None):
        assert "Dashboard" in _prompt
        assert "h1" in _prompt
        return json.dumps({"summary": "dashboard smoke", "checks": [{
            "kind": "element_text", "source_id": "web", "url": "/dashboard",
            "selector": "h1", "expected": "Revenue", "name": "dashboard title",
        }]})

    monkeypatch.setattr(builder, "_ask_llm", fake_ask)
    spec = ConnectionSpec(type=SourceType.WEB, name="dashboard", url="https://example.test")
    plan = run(builder.build_plan(None, [], connections={"web": spec}, use_llm=True))
    assert any(c.kind == CheckKind.ELEMENT_TEXT and c.expected == "Revenue" for c in plan.checks)


def test_ui_baseline_plan_is_bounded_and_rebaseline_is_explicit(monkeypatch) -> None:
    from asyncio import run

    class FakeBrowser:
        def __init__(self, _spec): pass
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def discover_routes(self): return [{"path": f"/r{i}", "title": ""} for i in range(2)]

    monkeypatch.setattr("app.connectors.browser.BrowserClient", FakeBrowser)
    monkeypatch.setattr("app.core.config.settings.run_max_checks", 10)
    spec = ConnectionSpec(type=SourceType.WEB, name="dashboard", url="https://example.test")
    plan = run(build_plan(None, [], use_llm=False, connections={"web": spec}, user_intent="rebaseline the visual snapshots"))
    assert len(plan.checks) <= 10
    assert any(c.kind == CheckKind.VISUAL_DIFF and c.params.get("rebaseline") for c in plan.checks)
    assert not _requests_rebaseline("Do not rebaseline the visual snapshots")
    assert _requests_rebaseline("Please rebaseline the visual snapshots")


def test_ui_plan_builder_rejects_cross_origin_and_unsafe_parity() -> None:
    web = ConnectionSpec(type=SourceType.WEB, name="dashboard", url="https://example.test")
    assert _coerce_ui({"kind": "page_load", "source_id": "web", "url": "https://evil.test"}, {"web": web}, []) is None
    assert _coerce_ui({"kind": "ui_parity", "source_id": "web", "url": "/", "selector": ".metric",
                       "params": {"query": "DELETE FROM metrics", "source_id": "warehouse"}}, {"web": web}, []) is None
    route_info = [{"path": "/", "selectors": ["main"]}]
    assert _coerce_ui({"kind": "element_present", "source_id": "web", "url": "/missing", "selector": "main"}, {"web": web}, [], route_info=route_info) is None
    assert _coerce_ui({"kind": "element_present", "source_id": "web", "url": "/", "selector": "#unknown"}, {"web": web}, [], route_info=route_info) is None
    assert _coerce_ui({"kind": "flow", "source_id": "web", "url": "/", "steps": [{"action": "goto", "value": "//evil.test/"}]}, {"web": web}, [], route_info=route_info) is None
    assert _coerce_ui({"kind": "visual_diff", "source_id": "web", "url": "/", "params": {"viewport": {"width": 100, "height": 100}}}, {"web": web}, [], route_info=route_info) is None


def test_merge_supports_tableless_checks() -> None:
    check = CheckSpec(name="load", kind=CheckKind.PAGE_LOAD, source_id="web", url="/", severity=Severity.BLOCKER)
    assert _merge([check], [check]) == [check]


def test_merge_keeps_distinct_ui_assertions_on_the_same_route() -> None:
    baseline = CheckSpec(name="main", kind=CheckKind.ELEMENT_PRESENT, source_id="web", url="/", selector="main")
    additional = CheckSpec(name="hero", kind=CheckKind.ELEMENT_PRESENT, source_id="web", url="/", selector="#hero")
    assert _merge([baseline], [additional]) == [baseline, additional]


def test_openui_serializes_tableless_ui_plan_and_results() -> None:
    from app.agent.openui import plan_program, report_program
    from app.schemas import CheckResult, CheckStatus, RunResult, TestPlan

    check = CheckSpec(name="loads", kind=CheckKind.PAGE_LOAD, source_id="web", url="/", severity=Severity.BLOCKER)
    plan = TestPlan(run_id="run-1", checks=[check])
    assert "table: \"/\"" in plan_program(plan)
    result = CheckResult(check_id=check.id, name=check.name, kind=check.kind, table=None,
                         url="/", severity=check.severity, status=CheckStatus.FAILED,
                         artifacts=["run-1/check-1/screenshot.png"])
    report = report_program(RunResult(id="run-1", plan=plan, results=[result]))
    assert "artifacts:" in report


def test_need_etl_question_can_intake_a_web_target() -> None:
    from app.agent.openui import question_program
    from app.routers.chat import _need_etl_question

    question = _need_etl_question()
    program = question_program(question)
    assert 'kind: "web_target"' in program


def test_browser_client_blocks_cross_origin_navigation() -> None:
    spec = ConnectionSpec(type=SourceType.WEB, name="dashboard", url="https://example.test")
    browser = BrowserClient(spec)
    with pytest.raises(BrowserError, match="outside the approved origin"):
        browser.allowed_url("https://evil.test/")


def test_browser_request_guard_aborts_cross_origin_redirects() -> None:
    class FakeRequest:
        url = "https://evil.test/redirected"

    class FakeRoute:
        request = FakeRequest()
        aborted = False

        def abort(self, *_args): self.aborted = True
        def continue_(self): raise AssertionError("cross-origin request was continued")

    browser = BrowserClient(ConnectionSpec(type=SourceType.WEB, name="app", url="https://example.test"))
    route = FakeRoute()
    browser._guard_request(route)
    assert route.aborted


def test_browser_origin_normalizes_default_ports() -> None:
    spec = ConnectionSpec(type=SourceType.WEB, name="app", url="https://example.test")
    assert BrowserClient(spec).allowed_url("https://example.test:443/health") == "https://example.test:443/health"


def test_extra_origins_allow_resources_but_not_flow_navigation() -> None:
    spec = ConnectionSpec(
        type=SourceType.WEB, name="app", url="https://example.test",
        options={"allowed_origins": ["https://cdn.example.test"]},
    )
    browser = BrowserClient(spec)
    assert browser.allowed_url("https://cdn.example.test/app.js") == "https://cdn.example.test/app.js"
    with pytest.raises(BrowserError, match="top-level navigation"):
        browser.navigation_url("https://cdn.example.test/login")


def test_browser_private_host_policy_checks_dns_results(monkeypatch) -> None:
    from socket import AF_INET, SOCK_STREAM

    monkeypatch.setattr("app.core.config.settings.ui_allow_private_hosts", False)
    monkeypatch.setattr(
        "app.connectors.browser.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(AF_INET, SOCK_STREAM, 6, "", ("10.10.0.5", 0))],
    )
    browser = BrowserClient(ConnectionSpec(type=SourceType.WEB, name="app", url="https://example.test"))
    with pytest.raises(BrowserError, match="private and loopback"):
        browser.allowed_url("/")


def test_artifact_paths_reject_user_controlled_traversal(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("app.core.config.settings.ui_artifact_dir", str(tmp_path))
    with pytest.raises(ValueError, match="identifier"):
        _artifact("run-1", "../../outside", "screenshot.png")
    with pytest.raises(ValueError, match="artifact name"):
        _artifact("run-1", "check-1", "../../outside.png")


@pytest.mark.parametrize(("text", "expected"), [("$1,204.50", (1204.5, False)), ("1.2K", (1200, False)), ("25%", (25, True))])
def test_displayed_number_normalization(text: str, expected: tuple[float, bool]) -> None:
    assert _display_number(text) == expected


def test_ignore_pattern_matching_is_bounded() -> None:
    event = "a" * 10_000 + "!"
    assert _ignored_events([event], ["(a+)+$"]) == [event]


@pytest.mark.asyncio
async def test_playwright_smoke_checks_against_local_fixture(tmp_path, monkeypatch) -> None:
    pytest.importorskip("playwright")
    try:
        await asyncio.to_thread(_require_chromium)
    except Exception as exc:
        pytest.skip(f"Chromium is unavailable: {exc}")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"<html><head><title>UI fixture</title></head><body><main>OK</main><script>console.error('fixture error')</script></body></html>"
            self.send_response(200)
            self.send_header("content-type", "text/html")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr("app.core.config.settings.ui_artifact_dir", str(tmp_path))
    spec = ConnectionSpec(type=SourceType.WEB, name="fixture", url=f"http://127.0.0.1:{server.server_port}")
    checks = [
        CheckSpec(name="loads", kind=CheckKind.PAGE_LOAD, source_id="web", url="/", severity=Severity.BLOCKER),
        CheckSpec(name="console clean", kind=CheckKind.CONSOLE_CLEAN, source_id="web", url="/", severity=Severity.WARNING),
    ]
    try:
        results = await execute_ui_checks(checks, {"web": spec}, "fixture-run")
    except Exception as exc:
        pytest.skip(f"Chromium is unavailable: {exc}")
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
    assert results[0].status.value == "passed"
    assert results[1].status.value == "failed"
    assert any(path.endswith("screenshot.png") for path in results[1].artifacts)


@pytest.mark.asyncio
async def test_all_ui_check_kinds_with_playwright_fixture(tmp_path, monkeypatch) -> None:
    pytest.importorskip("playwright")
    try:
        await asyncio.to_thread(_require_chromium)
    except Exception as exc:
        pytest.skip(f"Chromium is unavailable: {exc}")

    visual_state = {"version": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/404":
                self.send_response(404)
                self.end_headers()
                return
            title = "Dashboard" if self.path == "/dashboard" else "Fixture App"
            if self.path == "/visual":
                body = f"<main style='background: {'white' if visual_state['version'] == 0 else 'black'}'><h1>Visual {visual_state['version']}</h1></main>"
            elif self.path == "/dashboard":
                body = "<main><h1>Dashboard</h1><span id='metric'>1.2K</span><button id='action' onclick=\"document.querySelector('#result').textContent='done'\">Run</button><p id='result'></p></main>"
            else:
                body = "<main><h1>Home</h1><a href='/dashboard'>Dashboard</a></main>"
            document = f"<html><head><title>{title}</title></head><body>{body}<script>console.error('fixture error'); fetch('/404')</script></body></html>".encode()
            self.send_response(200)
            self.send_header("content-type", "text/html")
            self.send_header("content-length", str(len(document)))
            self.end_headers()
            self.wfile.write(document)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr("app.core.config.settings.ui_artifact_dir", str(tmp_path / "artifacts"))
    web = ConnectionSpec(type=SourceType.WEB, name="fixture", url=f"http://127.0.0.1:{server.server_port}")
    sqlite_path = tmp_path / "warehouse.db"
    with sqlite3.connect(sqlite_path) as database:
        database.execute("CREATE TABLE metrics (revenue INTEGER)")
        database.execute("INSERT INTO metrics VALUES (1200)")
    warehouse = ConnectionSpec(type=SourceType.SQLITE, name="warehouse", path=str(sqlite_path))
    checks = [
        CheckSpec(name="load", kind=CheckKind.PAGE_LOAD, source_id="web", url="/dashboard", params={"title_pattern": "Dashboard"}, severity=Severity.CRITICAL),
        CheckSpec(name="main visible", kind=CheckKind.ELEMENT_PRESENT, source_id="web", url="/dashboard", selector="main", severity=Severity.WARNING),
        CheckSpec(name="heading", kind=CheckKind.ELEMENT_TEXT, source_id="web", url="/dashboard", selector="h1", expected="Dashboard", severity=Severity.WARNING),
        CheckSpec(name="missing text", kind=CheckKind.ELEMENT_TEXT, source_id="web", url="/dashboard", selector="#does-not-exist", expected="missing", severity=Severity.WARNING),
        CheckSpec(name="console clean", kind=CheckKind.CONSOLE_CLEAN, source_id="web", url="/dashboard", severity=Severity.WARNING),
        CheckSpec(name="console ignore filter", kind=CheckKind.CONSOLE_CLEAN, source_id="web", url="/dashboard", params={"ignore_patterns": ["fixture error", "404"]}, severity=Severity.INFO),
        CheckSpec(name="network clean", kind=CheckKind.NETWORK_CLEAN, source_id="web", url="/dashboard", severity=Severity.WARNING),
        CheckSpec(name="network ignore filter", kind=CheckKind.NETWORK_CLEAN, source_id="web", url="/dashboard", params={"ignore_patterns": ["/404"], "settle_ms": 50}, severity=Severity.INFO),
        CheckSpec(name="warehouse parity", kind=CheckKind.UI_PARITY, source_id="web", url="/dashboard", selector="#metric", params={"query": "SELECT revenue FROM metrics", "source_id": "warehouse", "tolerance": 0}, severity=Severity.CRITICAL),
        CheckSpec(name="ambiguous parity query", kind=CheckKind.UI_PARITY, source_id="web", url="/dashboard", selector="#metric", params={"query": "SELECT revenue FROM metrics UNION ALL SELECT revenue FROM metrics", "source_id": "warehouse"}, severity=Severity.WARNING),
        CheckSpec(name="flow", kind=CheckKind.FLOW, source_id="web", url="/dashboard", steps=[{"action": "click", "selector": "#action"}, {"action": "assert_text", "selector": "#result", "value": "done"}], severity=Severity.INFO),
        CheckSpec(name="visual baseline", kind=CheckKind.VISUAL_DIFF, source_id="web", url="/visual", params={"viewport": {"width": 800, "height": 600}}, severity=Severity.INFO),
    ]
    try:
        def discover():
            with BrowserClient(web) as browser:
                return browser.discover_routes()

        route_info = await asyncio.to_thread(discover)
        dashboard = next(route for route in route_info if route["path"] == "/dashboard")
        assert "Dashboard" in dashboard["headings"]
        assert "h1" in dashboard["selectors"]
        results = await execute_ui_checks(checks, {"web": web, "warehouse": warehouse}, "all-kinds")
        visual_state["version"] = 1
        changed_visual = await execute_ui_checks(
            [CheckSpec(name="visual changed", kind=CheckKind.VISUAL_DIFF, source_id="web", url="/visual",
                       params={"viewport": {"width": 800, "height": 600}}, severity=Severity.INFO)],
            {"web": web}, "visual-change",
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
    by_name = {result.name: result for result in results}
    assert by_name["load"].status.value == "passed"
    assert by_name["main visible"].status.value == "passed"
    assert by_name["heading"].status.value == "passed"
    assert by_name["missing text"].status.value == "failed"
    assert by_name["console clean"].status.value == "failed"
    assert by_name["console ignore filter"].status.value == "passed"
    assert by_name["network clean"].status.value == "failed"
    assert by_name["network ignore filter"].status.value == "passed"
    assert by_name["warehouse parity"].status.value == "passed"
    assert by_name["ambiguous parity query"].status.value == "error"
    assert by_name["flow"].status.value == "passed"
    assert by_name["visual baseline"].status.value == "passed"
    assert changed_visual[0].status.value == "failed"
    assert any(path.endswith("diff.png") for path in changed_visual[0].artifacts)
