"""Playwright-backed browser sessions for frontend checks."""

from __future__ import annotations

import contextlib
import ipaddress
import socket
from typing import Any
from urllib.parse import urljoin, urlparse

from app.core.config import settings
from app.schemas import ConnectionSpec


class BrowserError(RuntimeError):
    pass


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise BrowserError("web target must be an http(s) URL")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, parsed.hostname.lower(), port


def _private_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost" or host.lower().endswith(".localhost")


def _safe_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}{parsed.path}"


class BrowserClient:
    """A synchronous Playwright context. Instantiate/use it within one worker thread."""

    def __init__(self, spec: ConnectionSpec):
        self.spec = spec
        self.base_url = (spec.url or "").rstrip("/")
        _origin(self.base_url)
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._pages: dict[str, Any] = {}
        self._console: dict[str, list[str]] = {}
        self._network: dict[str, list[str]] = {}
        self._status: dict[str, int | None] = {}

    def __enter__(self) -> BrowserClient:
        if not settings.ui_enabled:
            raise BrowserError("UI testing is disabled")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserError("Playwright is not installed; rebuild with the ui extra") from exc
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=True)
            context_args: dict[str, Any] = {"extra_http_headers": self.spec.headers or {}}
            if self.spec.auth_mode == "basic":
                context_args["http_credentials"] = {
                    "username": self.spec.auth_username or "",
                    "password": self.spec.auth_password.get_secret_value()
                    if self.spec.auth_password
                    else "",
                }
            context_args["service_workers"] = "block"
            self._context = self._browser.new_context(**context_args)
            # Route every request, not just the first goto(): redirects and links
            # reached by an approved flow must obey the same-origin boundary too.
            self._context.route("**/*", self._guard_request)
            if self.spec.auth_mode == "login":
                options = self.spec.options or {}
                login_path = str(options.get("login_path") or "/")
                page = self._context.new_page()
                page.goto(self.navigation_url(login_path), wait_until="domcontentloaded")
                page.locator(str(options.get("user_selector") or "input[name=username]")).fill(self.spec.auth_username or "")
                page.locator(str(options.get("pass_selector") or "input[type=password]")).fill(
                    self.spec.auth_password.get_secret_value() if self.spec.auth_password else ""
                )
                page.locator(str(options.get("submit_selector") or "button[type=submit]")).click()
                success = options.get("success_selector")
                if success:
                    page.locator(str(success)).wait_for(state="visible")
                page.close()
            return self
        except Exception:
            self.__exit__()
            raise

    def __exit__(self, *_exc: object) -> None:
        for obj in (self._context, self._browser, self._playwright):
            if obj is not None:
                with contextlib.suppress(Exception):
                    obj.close() if obj is not self._playwright else obj.stop()

    def allowed_url(self, path: str | None) -> str:
        target = urljoin(self.base_url + "/", path or "")
        if _origin(target) != _origin(self.base_url):
            allowed = self.spec.options.get("allowed_origins", [])
            if _origin(target) not in {_origin(x) for x in allowed}:
                raise BrowserError("navigation outside the approved origin is blocked")
        host = urlparse(target).hostname or ""
        if not settings.ui_allow_private_hosts and _private_host(host):
            raise BrowserError("private and loopback web targets are disabled")
        if host in {"169.254.169.254", "metadata.google.internal"}:
            raise BrowserError("cloud metadata endpoints are blocked")
        # Metadata hostnames can be aliased through DNS. Block the well-known
        # metadata address even when it is hidden behind an otherwise valid
        # same-origin hostname.
        try:
            addresses = {
                ipaddress.ip_address(entry[4][0])
                for entry in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
            }
            if ipaddress.ip_address("169.254.169.254") in addresses:
                raise BrowserError("cloud metadata endpoints are blocked")
            if not settings.ui_allow_private_hosts and any(
                address.is_private or address.is_loopback or address.is_link_local
                for address in addresses
            ):
                raise BrowserError("private and loopback web targets are disabled")
        except OSError:
            pass
        return target

    def navigation_url(self, path: str | None) -> str:
        """Top-level flow navigations stay on the app origin.

        Extra allowed origins are for resources and API calls. They do not
        widen where an approved flow can navigate or submit credentials.
        """
        target = self.allowed_url(path)
        if _origin(target) != _origin(self.base_url):
            raise BrowserError("top-level navigation outside the app origin is blocked")
        return target

    def _guard_request(self, route: Any) -> None:
        try:
            self.allowed_url(route.request.url)
        except Exception:
            route.abort("blockedbyclient")
            return
        route.continue_()

    def page(self, route: str | None = None):
        url = self.allowed_url(route)
        key = url
        if key in self._pages:
            return self._pages[key]
        page = self._context.new_page()
        page.set_default_timeout(settings.ui_check_timeout_seconds * 1000)
        page.set_default_navigation_timeout(settings.ui_nav_timeout_seconds * 1000)
        self._console[key], self._network[key] = [], []
        page.on("console", lambda msg: self._console[key].append(msg.text[:500]) if msg.type == "error" else None)
        page.on("pageerror", lambda err: self._console[key].append(str(err)[:500]))
        page.on("requestfailed", lambda req: self._network[key].append(f"{req.method} {_safe_url(req.url)}: {req.failure}"))
        page.on("response", lambda res: self._network[key].append(f"{res.status} {_safe_url(res.url)}") if res.status >= 400 else None)
        response = page.goto(url, wait_until="domcontentloaded")
        self._status[key] = response.status if response else None
        self._pages[key] = page
        return page

    def probe(self) -> dict:
        page = self.page()
        return {
            "status": self._status.get(self.base_url + "/"),
            "ok": (self._status.get(self.base_url + "/") or 0) < 400,
            "title": page.title(),
            "url": _safe_url(page.url),
            "routes": self.discover_routes(page),
        }

    def discover_routes(self, page=None) -> list[dict[str, Any]]:
        page = page or self.page()
        origin = _origin(self.base_url)
        base_path = urlparse(self.base_url).path.rstrip("/")
        root_route = f"{base_path}/" if base_path else "/"
        pending = [root_route]
        seen: set[str] = set()
        routes: list[dict[str, Any]] = []
        while pending and len(routes) < settings.ui_max_pages:
            path = pending.pop(0)
            if path in seen:
                continue
            seen.add(path)
            try:
                current = self.page(path)
                selectors = {"body", "main", "h1", "h2", "button", "form", "a[href]"}
                selectors.update(current.locator("[id]").evaluate_all("els => els.map(e => '#' + CSS.escape(e.id))"))
                selectors.update(
                    current.locator("input[name],select[name],textarea[name]").evaluate_all(
                        "els => els.map(e => e.tagName.toLowerCase() + '[name=\\\"' + e.name + '\\\"]')"
                    )
                )
                routes.append({
                    "path": path,
                    "title": current.title()[:200],
                    "headings": current.locator("h1,h2").evaluate_all("els => els.map(e => e.innerText.trim()).filter(Boolean).slice(0, 12)"),
                    "forms": current.locator("form").count(),
                    "buttons": current.locator("button").evaluate_all("els => els.map(e => e.innerText.trim()).filter(Boolean).slice(0, 12)"),
                    "selectors": sorted(selectors)[:100],
                })
                hrefs = current.locator("a[href]").evaluate_all("els => els.map(e => e.href)")
                for href in hrefs:
                    parsed = urlparse(href)
                    try:
                        if _origin(href) != origin or parsed.query or parsed.fragment:
                            continue
                        candidate = parsed.path or "/"
                        if base_path and candidate != base_path and not candidate.startswith(base_path + "/"):
                            continue
                        if candidate not in seen and candidate not in pending:
                            pending.append(candidate)
                    except Exception:
                        continue
            except Exception as exc:
                routes.append({"path": path, "title": "", "error": str(exc)[:200], "selectors": []})
        return routes

    def events(self, route: str | None) -> tuple[list[str], list[str]]:
        key = self.allowed_url(route)
        return self._console.get(key, []), self._network.get(key, [])

    def status_code(self, route: str | None) -> int | None:
        return self._status.get(self.allowed_url(route))
