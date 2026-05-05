from __future__ import annotations

import asyncio

from m365_copilot_openai_proxy.config import Settings
from m365_copilot_openai_proxy.models import AuthSessionSnapshot
from m365_copilot_openai_proxy.playwright_refresh import (
    AuthenticatedSessionState,
    _advance_login_flow,
    _build_auth_snapshot,
    _capture_token,
    _parse_storage_auth_record,
    login_with_playwright,
    open_authenticated_context,
    run_refresh_daemon,
)

_TEST_JWT = (
    "eyJhbGciOiJub25lIn0."
    "eyJvaWQiOiIxMjM0NTY3OC0xMjM0LTEyMzQtMTIzNC0xMjM0NTY3ODkwYWIiLCJ0aWQiOiJhYmNkZWYwMS0yMzQ1LTY3ODktYWJjZC1lZjAxMjM0NTY3ODkiLCJleHAiOjQxMDAwMDAwMDB9."
)


class FakePage:
    def __init__(self, evaluate_values: list[object]):
        self._evaluate_values = list(evaluate_values)
        self.events: dict[str, object] = {}
        self.goto_calls: list[tuple[str, str]] = []
        self.locator_calls: list[str] = []
        self.clicked_selectors: list[str] = []
        self.clicked_texts: list[str] = []
        self.typed_text: list[str] = []
        self.pressed_keys: list[str] = []
        self.url = "https://m365.cloud.microsoft/chat"
        self.wait_calls: list[int] = []
        self.body_text = ""
        self.selector_counts: dict[str, int] = {}
        self.text_counts: dict[str, int] = {}
        self.click_hooks: dict[str, object] = {}
        self.text_click_hooks: dict[str, object] = {}
        self.filled_values: list[str] = []

    def on(self, event: str, handler) -> None:
        self.events[event] = handler

    async def goto(self, url: str, wait_until: str) -> None:
        self.goto_calls.append((url, wait_until))

    async def evaluate(self, _script: str):
        if self._evaluate_values:
            return self._evaluate_values.pop(0)
        return None

    def locator(self, selector: str):
        self.locator_calls.append(selector)
        return FakeLocator(self, selector)

    def get_by_text(self, text: str, exact: bool = True):
        return FakeTextLocator(self, text, exact)

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


class FakeTextLocator:
    def __init__(self, page: FakePage, text: str, exact: bool):
        self.page = page
        self.text = text
        self.exact = exact

    @property
    def first(self):
        return self

    async def count(self) -> int:
        if self.text in self.page.text_counts:
            return self.page.text_counts[self.text]
        if self.exact and self.text in self.page.body_text:
            return 1
        return 0

    async def click(self, timeout: int | None = None) -> None:
        self.page.clicked_texts.append(f"{self.text}:{timeout}")
        hook = self.page.text_click_hooks.get(self.text)
        if callable(hook):
            hook()


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


def install_fake_playwright(monkeypatch, page: FakePage) -> tuple[FakeContext, FakePlaywrightManager]:
    context = FakeContext(page)
    manager = FakePlaywrightManager(context)
    monkeypatch.setattr(
        "m365_copilot_openai_proxy.playwright_refresh._import_playwright",
        lambda: (lambda: manager),
    )
    return context, manager


def test_capture_token_keeps_polling_after_wait_timeout(monkeypatch, tmp_path) -> None:
    page = FakePage(
        [
            None,
            {
                "secret": _TEST_JWT,
                "expiresOn": 4100000000,
                "target": "https://www.office.com/v2/M365Copilot.Read.All",
            },
        ]
    )
    page.url = "https://m365.cloud.microsoft/chat"
    install_fake_playwright(monkeypatch, page)
    clock = FakeClock()
    monkeypatch.setattr("m365_copilot_openai_proxy.playwright_refresh.time.time", clock.time)

    async def fake_wait_for(awaitable, timeout: float):
        if hasattr(awaitable, "close"):
            awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr("m365_copilot_openai_proxy.playwright_refresh.asyncio.wait_for", fake_wait_for)

    settings = Settings(
        _env_file=None,
        M365_ACCOUNT_MODE="enterprise",
        M365_PROFILE_DIR=str(tmp_path / "profile"),
    )
    token = asyncio.run(_capture_token(settings, headed=False, timeout_seconds=3))

    assert token == _TEST_JWT
    assert page.goto_calls == [(settings.login_url, "domcontentloaded")]
    assert page.clicked_selectors
    assert page.typed_text
    assert page.pressed_keys


def test_advance_login_flow_clicks_enterprise_account_picker_tile() -> None:
    page = FakePage([])
    page.url = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    page.selector_counts['div[role="button"][data-test-id]'] = 1

    def after_click():
        page.url = "https://m365.cloud.microsoft/chat"

    page.click_hooks['div[role="button"][data-test-id]'] = after_click

    settings = Settings(_env_file=None, M365_ACCOUNT_MODE="enterprise")
    asyncio.run(_advance_login_flow(page, settings))

    assert page.clicked_selectors == ['div[role="button"][data-test-id]:5000']
    assert page.wait_calls == [2000]


def test_advance_login_flow_personal_clicks_password_path_and_stay_signed_in() -> None:
    page = FakePage([])
    page.url = "https://login.live.com/oauth20_authorize.srf"
    page.body_text = "Sign in another way Use your password"
    page.selector_counts['input[type="password"]'] = 0
    page.selector_counts['button[type="submit"]'] = 0
    page.text_counts["Use your password"] = 1
    page.text_counts["Yes"] = 1
    settings = Settings(
        _env_file=None,
        M365_ACCOUNT_MODE="personal",
        M365_LOGIN_EMAIL="user@example.com",
        M365_LOGIN_PASSWORD="secret",
        M365_LOGIN_TOTP_SECRET="JBSWY3DPEHPK3PXP",
    )

    asyncio.run(_advance_login_flow(page, settings))
    page.body_text = "Enter your password"
    page.selector_counts['input[type="password"]'] = 1
    page.selector_counts['button[type="submit"]'] = 1
    asyncio.run(_advance_login_flow(page, settings))
    page.selector_counts['input[type="password"]'] = 0
    page.selector_counts['button[type="submit"]'] = 0
    page.body_text = "Stay signed in? Yes No"
    asyncio.run(_advance_login_flow(page, settings))

    assert "Use your password:5000" in page.clicked_texts
    assert 'input[type="password"]:secret' in page.filled_values
    assert 'button[type="submit"]:5000' in page.clicked_selectors
    assert "Yes:5000" in page.clicked_texts


def test_open_authenticated_context_rejects_account_mode_mismatch(monkeypatch, tmp_path) -> None:
    page = FakePage([None])
    page.url = "https://login.live.com/oauth20_authorize.srf"
    install_fake_playwright(monkeypatch, page)
    clock = FakeClock()
    monkeypatch.setattr("m365_copilot_openai_proxy.playwright_refresh.time.time", clock.time)

    async def fake_wait_for(awaitable, timeout: float):
        if hasattr(awaitable, "close"):
            awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr("m365_copilot_openai_proxy.playwright_refresh.asyncio.wait_for", fake_wait_for)

    settings = Settings(
        _env_file=None,
        M365_ACCOUNT_MODE="enterprise",
        M365_PROFILE_DIR=str(tmp_path / "profile"),
    )

    try:
        asyncio.run(open_authenticated_context(settings, headed=False, timeout_seconds=2))
    except Exception as exc:
        assert "M365_ACCOUNT_MODE is enterprise" in str(exc)
    else:
        raise AssertionError("Expected account mode mismatch error.")


def test_open_authenticated_context_captures_personal_auth_snapshot(monkeypatch, tmp_path) -> None:
    page = FakePage(
        [
            {
                "secret": "substrate-rest-token",
                "expiresOn": 4100000000,
                "target": "https://substrate.office.com/.default https://substrate.office.com/M365.Access",
            },
            {
                "secret": "substrate-rest-token",
                "expiresOn": 4100000000,
                "target": "https://substrate.office.com/.default https://substrate.office.com/M365.Access",
            },
            {
                "access_token": "graph-token",
                "expires_in": 300,
            },
            {
                "access_token": "search-token",
                "expires_in": 120,
            },
        ]
    )
    page.url = "https://login.live.com/oauth20_authorize.srf"
    page.body_text = "Stay signed in? Yes No"
    page.text_counts["Yes"] = 1

    def click_yes():
        page.url = "https://m365.cloud.microsoft/chat"
        page.events["websocket"](
            type(
                "FakeWebSocket",
                (),
                {
                    "url": "wss://substrate.office.com/m365Copilot/Chathub/"
                    "00000000-0000-0000-853e-527a6bf3c11e@84df9e7f-e9f6-40af-b435-aaaaaaaaaaaa"
                    "?access_token=eyJhbGciOiJkaXIifQ.test.encrypted.value.more",
                },
            )()
        )

    page.text_click_hooks["Yes"] = click_yes
    context, manager = install_fake_playwright(monkeypatch, page)
    settings = Settings(
        _env_file=None,
        M365_ACCOUNT_MODE="personal",
        M365_PROFILE_DIR=str(tmp_path / "profile"),
    )

    returned_manager, returned_context, _page, auth_session, auth_state = asyncio.run(
        open_authenticated_context(settings, headed=False, timeout_seconds=2)
    )

    assert returned_manager is manager
    assert returned_context is context
    assert auth_session.account_mode == "personal"
    assert auth_session.access_token == "substrate-rest-token"
    assert auth_session.expires_at == 4100000000
    assert auth_session.oid == "00000000-0000-0000-853e-527a6bf3c11e"
    assert auth_session.tid == "84df9e7f-e9f6-40af-b435-aaaaaaaaaaaa"
    assert auth_session.graph_access_token == "graph-token"
    assert auth_session.graph_expires_at is not None
    assert auth_session.search_access_token == "search-token"
    assert auth_session.search_expires_at is not None
    assert auth_state.saw_login_host is True
    assert auth_state.detected_account_mode == "personal"


def test_parse_storage_auth_record_accepts_opaque_secret() -> None:
    record = _parse_storage_auth_record(
        {
            "secret": "opaque-token",
            "expiresOn": "4100000000",
            "target": "https://substrate.office.com/.default",
        }
    )
    assert record == {
        "access_token": "opaque-token",
        "expires_at": 4100000000,
        "target": "https://substrate.office.com/.default",
    }


def test_build_auth_snapshot_personal_overrides_websocket_token_with_storage_token() -> None:
    settings = Settings(_env_file=None, M365_ACCOUNT_MODE="personal")
    snapshot = _build_auth_snapshot(
        settings,
        {
            "access_token": "substrate-rest-token",
            "expires_at": 4100000000,
            "websocket_url": (
                "wss://substrate.office.com/m365Copilot/Chathub/"
                "00000000-0000-0000-853e-527a6bf3c11e@84df9e7f-e9f6-40af-b435-aaaaaaaaaaaa"
                "?access_token=eyJhbGciOiJkaXIifQ.ws.token"
            ),
        },
    )
    assert snapshot is not None
    assert snapshot.account_mode == "personal"
    assert snapshot.access_token == "substrate-rest-token"
    assert snapshot.websocket_url is not None


def test_refresh_daemon_forces_refresh_on_startup(monkeypatch, tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        M365_ACCOUNT_MODE="enterprise",
        M365_AUTH_STATE_FILE=str(tmp_path / "auth_session.json"),
        M365_ACCESS_TOKEN_FILE=str(tmp_path / "access_token.txt"),
        M365_TOKEN_CAPTURE_TIMEOUT_SECONDS=456,
        M365_TOKEN_REFRESH_BUFFER_SECONDS=300,
    )
    auth_session = AuthSessionSnapshot(
        account_mode="enterprise",
        access_token="existing-token",
        expires_at=4100000000,
        captured_at=123,
        oid="oid",
        tid="tid",
    )
    calls: list[str] = []
    refresh_timeouts: list[int] = []

    def fake_load_auth_session(_settings: Settings) -> AuthSessionSnapshot:
        calls.append("load")
        return auth_session

    async def fake_refresh_token_with_playwright(_settings: Settings, *, headed: bool = False, timeout_seconds: int = 90):
        calls.append("refresh")
        refresh_timeouts.append(timeout_seconds)
        return tmp_path / "auth_session.json"

    def fake_auth_session_needs_refresh(_auth_session: AuthSessionSnapshot, _buffer_seconds: int) -> bool:
        calls.append("needs-refresh")
        return False

    def fake_auth_session_expires_at(_auth_session: AuthSessionSnapshot) -> int | None:
        return 9999999999

    class StopDaemon(Exception):
        pass

    async def fake_sleep(_delay: float) -> None:
        raise StopDaemon()

    monkeypatch.setattr("m365_copilot_openai_proxy.playwright_refresh.load_auth_session", fake_load_auth_session)
    monkeypatch.setattr("m365_copilot_openai_proxy.playwright_refresh.refresh_token_with_playwright", fake_refresh_token_with_playwright)
    monkeypatch.setattr("m365_copilot_openai_proxy.playwright_refresh.auth_session_needs_refresh", fake_auth_session_needs_refresh)
    monkeypatch.setattr("m365_copilot_openai_proxy.playwright_refresh.auth_session_expires_at", fake_auth_session_expires_at)
    monkeypatch.setattr("m365_copilot_openai_proxy.playwright_refresh.asyncio.sleep", fake_sleep)

    try:
        asyncio.run(run_refresh_daemon(settings))
    except StopDaemon:
        pass

    assert calls[:3] == ["load", "refresh", "load"]
    assert refresh_timeouts == [settings.token_capture_timeout_seconds]


def test_login_with_playwright_runs_model_probe_after_personal_reauth(monkeypatch, tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        M365_ACCOUNT_MODE="personal",
        M365_AUTH_STATE_FILE=str(tmp_path / "auth_session.json"),
        M365_ACCESS_TOKEN_FILE=str(tmp_path / "access_token.txt"),
        M365_AUTO_PROBE_MODELS_ON_LOGIN=True,
    )
    page = FakePage([])
    context = FakeContext(page)
    manager = FakePlaywrightManager(context)
    probe_calls: list[tuple[FakeContext, bool]] = []
    saved: list[AuthSessionSnapshot] = []
    auth_session = AuthSessionSnapshot(
        account_mode="personal",
        access_token="eyJ.personal",
        expires_at=4100000000,
        captured_at=123,
        oid="oid",
        tid="tid",
        websocket_url="wss://substrate.office.com/m365Copilot/Chathub/oid@tid?access_token=eyJ.personal",
    )

    async def fake_open_authenticated_context(*args, **kwargs):
        return manager, context, page, auth_session, AuthenticatedSessionState(
            saw_login_host=True,
            detected_account_mode="personal",
        )

    async def fake_probe_impl(*, context, settings, prompt=None):
        probe_calls.append((context, settings.auto_probe_models_on_login))

    def fake_write_auth_session(_settings: Settings, value: AuthSessionSnapshot):
        saved.append(value)
        path = tmp_path / "auth_session.json"
        path.write_text("{}", encoding="utf-8")
        return path

    monkeypatch.setattr(
        "m365_copilot_openai_proxy.playwright_refresh.open_authenticated_context",
        fake_open_authenticated_context,
    )
    monkeypatch.setattr(
        "m365_copilot_openai_proxy.playwright_model_probe.maybe_auto_probe_models_in_context",
        fake_probe_impl,
    )
    monkeypatch.setattr(
        "m365_copilot_openai_proxy.playwright_refresh.write_auth_session",
        fake_write_auth_session,
    )

    path = asyncio.run(login_with_playwright(settings, headed=False))

    assert path == tmp_path / "auth_session.json"
    assert saved == [auth_session]
    assert probe_calls == [(context, True)]
    assert context.closed is True
    assert manager.closed is True
