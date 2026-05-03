from __future__ import annotations

import asyncio

from m365_copilot_openai_proxy.config import Settings
from m365_copilot_openai_proxy.playwright_refresh import (
    _advance_login_flow,
    _capture_token,
    login_with_playwright,
    run_refresh_daemon,
)
from m365_copilot_openai_proxy.token_store import TokenStoreError


class FakePage:
    def __init__(self, evaluate_values: list[str | None]):
        self._evaluate_values = list(evaluate_values)
        self.events: dict[str, object] = {}
        self.goto_calls: list[tuple[str, str]] = []
        self.locator_calls: list[str] = []
        self.clicked_selectors: list[str] = []
        self.typed_text: list[str] = []
        self.pressed_keys: list[str] = []
        self.url = "https://m365.cloud.microsoft/chat"
        self.wait_calls: list[int] = []
        self.body_text = ""
        self.selector_counts: dict[str, int] = {}
        self.click_hooks: dict[str, object] = {}
        self.filled_values: list[str] = []

    def on(self, event: str, handler) -> None:
        self.events[event] = handler

    async def goto(self, url: str, wait_until: str) -> None:
        self.goto_calls.append((url, wait_until))

    async def evaluate(self, _script: str) -> str | None:
        if self._evaluate_values:
            return self._evaluate_values.pop(0)
        return None

    def locator(self, selector: str):
        self.locator_calls.append(selector)
        return FakeLocator(self, selector)

    async def wait_for_timeout(self, timeout_ms: int) -> None:
        self.wait_calls.append(timeout_ms)


class FakeLocator:
    def __init__(self, page: FakePage, selector: str):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    async def count(self) -> int:
        return self.page.selector_counts.get(self.selector, 0)

    async def click(self, timeout: int | None = None) -> None:
        self.page.clicked_selectors.append(f"{self.selector}:{timeout}")
        hook = self.page.click_hooks.get(self.selector)
        if callable(hook):
            hook()

    async def fill(self, value: str) -> None:
        self.page.filled_values.append(f"{self.selector}:{value}")

    async def press_sequentially(self, text: str, delay: int | None = None) -> None:
        self.page.typed_text.append(f"{self.selector}:{text}:{delay}")

    async def press(self, key: str) -> None:
        self.page.pressed_keys.append(f"{self.selector}:{key}")

    async def inner_text(self) -> str:
        if self.selector == "body":
            return self.page.body_text
        return ""


class FakeContext:
    def __init__(self, page: FakePage):
        self.pages = [page]
        self.closed = False
        self.page_handler = None

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
    assert page.clicked_selectors
    assert page.typed_text
    assert page.pressed_keys


def test_advance_login_flow_clicks_account_picker_tile() -> None:
    page = FakePage([])
    page.url = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    page.selector_counts['div[role="button"][data-test-id]'] = 1

    def after_click():
        page.url = "https://m365.cloud.microsoft/chat"

    page.click_hooks['div[role="button"][data-test-id]'] = after_click

    settings = Settings(_env_file=None)
    asyncio.run(_advance_login_flow(page, settings))

    assert page.clicked_selectors == ['div[role="button"][data-test-id]:5000']
    assert page.wait_calls == [2000]


def test_advance_login_flow_raises_on_password_prompt() -> None:
    page = FakePage([])
    page.url = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    page.body_text = "Enter password\nForgot my password"

    try:
        settings = Settings(_env_file=None)
        asyncio.run(_advance_login_flow(page, settings))
    except Exception as exc:
        assert str(exc) == (
            "Saved Playwright profile needs interactive reauthentication: "
            "Microsoft is prompting for the account password."
        )
    else:
        raise AssertionError("Expected interactive reauthentication error.")


def test_advance_login_flow_allows_manual_password_prompt_when_requested() -> None:
    page = FakePage([])
    page.url = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    page.body_text = "Enter password\nForgot my password"

    settings = Settings(_env_file=None)
    asyncio.run(_advance_login_flow(page, settings, allow_manual_reauth=True))


def test_advance_login_flow_fills_email_page_when_credentials_exist() -> None:
    page = FakePage([])
    page.url = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    page.selector_counts["#i0116"] = 1
    page.selector_counts["#idSIButton9"] = 1
    settings = Settings(
        _env_file=None,
        M365_LOGIN_EMAIL="user@example.com",
        M365_LOGIN_PASSWORD="secret",
        M365_LOGIN_TOTP_SECRET="JBSWY3DPEHPK3PXP",
    )

    asyncio.run(_advance_login_flow(page, settings))

    assert page.filled_values == ["#i0116:user@example.com"]
    assert page.clicked_selectors == ["#i0116:1000", "#idSIButton9:5000"]


def test_advance_login_flow_fills_password_page_when_credentials_exist() -> None:
    page = FakePage([])
    page.url = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    page.selector_counts["#i0118"] = 1
    page.selector_counts["#idSIButton9"] = 1
    settings = Settings(
        _env_file=None,
        M365_LOGIN_EMAIL="user@example.com",
        M365_LOGIN_PASSWORD="secret",
        M365_LOGIN_TOTP_SECRET="JBSWY3DPEHPK3PXP",
    )

    asyncio.run(_advance_login_flow(page, settings))

    assert page.filled_values == ["#i0118:secret"]
    assert page.clicked_selectors == ["#i0118:1000", "#idSIButton9:5000"]


def test_refresh_daemon_forces_refresh_on_startup(monkeypatch, tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        M365_ACCESS_TOKEN_FILE=str(tmp_path / "access_token.txt"),
        M365_TOKEN_CAPTURE_TIMEOUT_SECONDS=456,
        M365_TOKEN_REFRESH_BUFFER_SECONDS=300,
    )
    settings_path = tmp_path / "access_token.txt"
    settings_path.write_text("existing-token\n", encoding="utf-8")
    calls: list[str] = []
    refresh_timeouts: list[int] = []

    def fake_load_access_token(_settings: Settings) -> str:
        calls.append("load")
        return settings_path.read_text(encoding="utf-8").strip()

    async def fake_refresh_token_with_playwright(_settings: Settings, *, headed: bool = False, timeout_seconds: int = 90):
        calls.append("refresh")
        refresh_timeouts.append(timeout_seconds)
        settings_path.write_text("new-token\n", encoding="utf-8")
        return settings_path

    def fake_token_needs_refresh(_token: str, _buffer_seconds: int) -> bool:
        calls.append("needs-refresh")
        return False

    def fake_token_expires_at(_token: str) -> int:
        return 9999999999

    class StopDaemon(Exception):
        pass

    async def fake_sleep(_delay: float) -> None:
        raise StopDaemon()

    monkeypatch.setattr("m365_copilot_openai_proxy.playwright_refresh.load_access_token", fake_load_access_token)
    monkeypatch.setattr("m365_copilot_openai_proxy.playwright_refresh.refresh_token_with_playwright", fake_refresh_token_with_playwright)
    monkeypatch.setattr("m365_copilot_openai_proxy.playwright_refresh.token_needs_refresh", fake_token_needs_refresh)
    monkeypatch.setattr("m365_copilot_openai_proxy.playwright_refresh.token_expires_at", fake_token_expires_at)
    monkeypatch.setattr("m365_copilot_openai_proxy.playwright_refresh.asyncio.sleep", fake_sleep)

    try:
        asyncio.run(run_refresh_daemon(settings))
    except StopDaemon:
        pass

    assert calls[:3] == ["load", "refresh", "load"]
    assert refresh_timeouts == [settings.token_capture_timeout_seconds]


def test_refresh_daemon_retries_startup_refresh_until_success(monkeypatch, tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        M365_ACCESS_TOKEN_FILE=str(tmp_path / "access_token.txt"),
        M365_TOKEN_REFRESH_RETRY_SECONDS=30,
    )
    settings_path = tmp_path / "access_token.txt"
    settings_path.write_text("existing-token\n", encoding="utf-8")
    refresh_attempts = {"count": 0}
    sleep_delays: list[float] = []

    def fake_load_access_token(_settings: Settings) -> str:
        return settings_path.read_text(encoding="utf-8").strip()

    async def fake_refresh_token_with_playwright(_settings: Settings, *, headed: bool = False, timeout_seconds: int = 90):
        refresh_attempts["count"] += 1
        if refresh_attempts["count"] == 1:
            raise RuntimeError("boom")
        settings_path.write_text("new-token\n", encoding="utf-8")
        return settings_path

    def fake_token_needs_refresh(_token: str, _buffer_seconds: int) -> bool:
        return False

    def fake_token_expires_at(_token: str) -> int:
        return 9999999999

    class StopDaemon(Exception):
        pass

    async def fake_sleep(delay: float) -> None:
        sleep_delays.append(delay)
        if len(sleep_delays) >= 2:
            raise StopDaemon()

    monkeypatch.setattr("m365_copilot_openai_proxy.playwright_refresh.load_access_token", fake_load_access_token)
    monkeypatch.setattr("m365_copilot_openai_proxy.playwright_refresh.refresh_token_with_playwright", fake_refresh_token_with_playwright)
    monkeypatch.setattr("m365_copilot_openai_proxy.playwright_refresh.token_needs_refresh", fake_token_needs_refresh)
    monkeypatch.setattr("m365_copilot_openai_proxy.playwright_refresh.token_expires_at", fake_token_expires_at)
    monkeypatch.setattr("m365_copilot_openai_proxy.playwright_refresh.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(
        "m365_copilot_openai_proxy.playwright_refresh.PlaywrightRefreshError",
        RuntimeError,
    )

    try:
        asyncio.run(run_refresh_daemon(settings))
    except StopDaemon:
        pass

    assert refresh_attempts["count"] == 2
    assert sleep_delays[0] == settings.token_refresh_retry_seconds
