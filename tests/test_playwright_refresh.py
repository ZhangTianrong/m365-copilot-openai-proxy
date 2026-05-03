from __future__ import annotations

import asyncio
import json

from m365_copilot_openai_proxy.config import Settings
from m365_copilot_openai_proxy.playwright_refresh import (
    _capture_token,
    export_cookies_with_playwright,
    import_cookies_with_playwright,
    load_cookies,
    login_with_playwright,
)


class FakePage:
    def __init__(self, evaluate_values: list[str | None]):
        self._evaluate_values = list(evaluate_values)
        self.events: dict[str, object] = {}
        self.goto_calls: list[tuple[str, str]] = []

    def on(self, event: str, handler) -> None:
        self.events[event] = handler

    async def goto(self, url: str, wait_until: str) -> None:
        self.goto_calls.append((url, wait_until))

    async def evaluate(self, _script: str) -> str | None:
        if self._evaluate_values:
            return self._evaluate_values.pop(0)
        return None


class FakeContext:
    def __init__(self, page: FakePage):
        self.pages = [page]
        self.closed = False
        self.cookies: list[dict[str, object]] | None = None
        self.existing_cookies: list[dict[str, object]] = []
        self.page_handler = None

    async def add_cookies(self, cookies: list[dict[str, object]]) -> None:
        self.cookies = cookies

    async def cookies(self) -> list[dict[str, object]]:
        return list(self.existing_cookies)

    async def new_page(self) -> FakePage:
        return self.pages[0]

    def on(self, event: str, handler) -> None:
        if event == "page":
            self.page_handler = handler

    async def close(self) -> None:
        self.closed = True


class FakePlaywright:
    def __init__(self, context: FakeContext):
        self.chromium = self
        self._context = context

    async def launch_persistent_context(self, **_kwargs) -> FakeContext:
        return self._context


class FakePlaywrightManager:
    def __init__(self, context: FakeContext):
        self._context = context
        self.closed = False

    async def __aenter__(self) -> FakePlaywright:
        return FakePlaywright(self._context)

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.closed = True


class FakeClock:
    def __init__(self) -> None:
        self.current = 0.0

    def time(self) -> float:
        self.current += 0.25
        return self.current


def install_fake_playwright(monkeypatch, page: FakePage) -> FakeContext:
    context = FakeContext(page)
    manager = FakePlaywrightManager(context)
    monkeypatch.setattr(
        "m365_copilot_openai_proxy.playwright_refresh._import_playwright",
        lambda: (lambda: manager),
    )
    return context


def test_capture_token_keeps_polling_after_wait_timeout(monkeypatch, tmp_path) -> None:
    page = FakePage([None, "access_token=eyJ.fake"])
    install_fake_playwright(monkeypatch, page)
    clock = FakeClock()
    monkeypatch.setattr("m365_copilot_openai_proxy.playwright_refresh.time.time", clock.time)

    async def fake_wait_for(awaitable, timeout: float):
        if hasattr(awaitable, "close"):
            awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr("m365_copilot_openai_proxy.playwright_refresh.asyncio.wait_for", fake_wait_for)

    settings = Settings(_env_file=None, M365_PROFILE_DIR=str(tmp_path / "profile"))
    token = asyncio.run(_capture_token(settings, headed=False, timeout_seconds=3))

    assert token == "eyJ.fake"
    assert page.goto_calls == [(settings.login_url, "domcontentloaded")]


def test_load_cookies_normalizes_browser_export(tmp_path) -> None:
    cookies_path = tmp_path / "cookies.json"
    cookies_path.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "M365Auth",
                        "value": "secret",
                        "domain": ".microsoft.com",
                        "path": "/",
                        "expirationDate": 1893456000,
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "no_restriction",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    cookies = load_cookies(cookies_path)

    assert cookies == [
        {
            "name": "M365Auth",
            "value": "secret",
            "domain": ".microsoft.com",
            "path": "/",
            "expires": 1893456000.0,
            "httpOnly": True,
            "secure": True,
            "sameSite": "None",
        }
    ]


def test_login_with_cookies_imports_into_profile_and_saves_token(monkeypatch, tmp_path) -> None:
    page = FakePage(["eyJ.cookie-token"])
    context = install_fake_playwright(monkeypatch, page)

    cookies_path = tmp_path / "cookies.json"
    cookies_path.write_text(
        json.dumps([{"name": "session", "value": "abc", "domain": ".microsoft.com", "path": "/"}]),
        encoding="utf-8",
    )

    settings = Settings(
        _env_file=None,
        M365_PROFILE_DIR=str(tmp_path / "profile"),
        M365_ACCESS_TOKEN_FILE=str(tmp_path / "access_token.txt"),
    )

    token_path = asyncio.run(
        login_with_playwright(
            settings,
            timeout_seconds=3,
            headed=False,
            cookies_path=cookies_path,
        )
    )

    assert context.cookies == [
        {
            "name": "session",
            "value": "abc",
            "domain": ".microsoft.com",
            "path": "/",
        }
    ]
    assert token_path.read_text(encoding="utf-8") == "eyJ.cookie-token\n"


def test_import_cookies_with_playwright_seeds_profile(monkeypatch, tmp_path) -> None:
    page = FakePage([])
    context = install_fake_playwright(monkeypatch, page)

    cookies_path = tmp_path / "cookies.json"
    cookies_path.write_text(
        json.dumps([{"name": "session", "value": "abc", "url": "https://m365.cloud.microsoft/"}]),
        encoding="utf-8",
    )

    settings = Settings(_env_file=None, M365_PROFILE_DIR=str(tmp_path / "profile"))
    count = asyncio.run(import_cookies_with_playwright(settings, cookies_path))

    assert count == 1
    assert context.cookies == [
        {
            "name": "session",
            "value": "abc",
            "url": "https://m365.cloud.microsoft/",
        }
    ]


def test_export_cookies_with_playwright_writes_profile_snapshot(monkeypatch, tmp_path) -> None:
    page = FakePage([])
    context = install_fake_playwright(monkeypatch, page)
    context.existing_cookies = [
        {
            "name": "session",
            "value": "abc",
            "domain": ".microsoft.com",
            "path": "/",
            "expires": 1893456000,
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
        }
    ]

    settings = Settings(_env_file=None, M365_PROFILE_DIR=str(tmp_path / "profile"))
    output_path, count = asyncio.run(
        export_cookies_with_playwright(settings, tmp_path / "cookies-export.json")
    )

    assert count == 1
    assert output_path.read_text(encoding="utf-8") == json.dumps(context.existing_cookies, indent=2) + "\n"
