from __future__ import annotations

import asyncio
import contextlib
import re
import time
from pathlib import Path

from .config import Settings
from .token_store import (
    TokenStoreError,
    load_access_token,
    token_expires_at,
    token_needs_refresh,
    write_access_token,
)

_STORAGE_JS = """
(() => {
    const stores = [sessionStorage, localStorage];
    for (const store of stores) {
        for (const key of Object.keys(store)) {
            if (!key.includes("accesstoken")) continue;
            try {
                const value = JSON.parse(store.getItem(key));
                if (value && value.secret && value.secret.startsWith("eyJ") &&
                    value.target && value.target.includes("substrate")) {
                    return value.secret;
                }
            } catch {}
        }
    }
    return null;
})()
"""

_CHATBOX_SELECTORS = (
    'textarea',
    '[role="textbox"]',
    '[contenteditable="true"]',
)
_EMAIL_SELECTOR = "#i0116"
_PASSWORD_SELECTOR = "#i0118"
_SUBMIT_SELECTOR = "#idSIButton9"
_USE_ANOTHER_ACCOUNT_SELECTOR = "#otherTile"
_ACCOUNT_TILE_SELECTOR = 'div[role="button"][data-test-id]'
_TOTP_SELECTORS = (
    "#idTxtBx_SAOTCC_OTC",
    'input[name="otc"]',
    'input[autocomplete="one-time-code"]',
    'input[inputmode="numeric"]',
)
_TOTP_CONTINUE_SELECTORS = (
    "#idSubmit_SAOTCC_Continue",
    "#idSIButton9",
)


class PlaywrightRefreshError(RuntimeError):
    pass


def _import_playwright():
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise PlaywrightRefreshError(
            "Playwright is not installed. Install the refresh extra, for example "
            "`pip install .[refresh]`, before using login or refresh commands."
        ) from exc
    return async_playwright


def _extract_token(raw: str | None) -> str | None:
    if not raw:
        return None
    match = re.search(r"access_token=([^&\s]+)", raw)
    token = match.group(1) if match else raw
    token = token.strip()
    return token if token.startswith("eyJ") else None


def _credentials_available(settings: Settings) -> bool:
    return bool(settings.login_email and settings.login_password and settings.login_totp_secret)


def _body_mentions_totp(body_text: str) -> bool:
    normalized = body_text.lower()
    return any(
        phrase in normalized
        for phrase in (
            "enter code",
            "verification code",
            "authenticator app",
            "one-time password",
            "totp",
        )
    )


def _generate_totp_code(secret: str) -> str:
    try:
        import pyotp
    except ImportError as exc:
        raise PlaywrightRefreshError(
            "pyotp is not installed. Install the refresh extra before using TOTP-based login."
        ) from exc

    normalized_secret = "".join(secret.split())
    return pyotp.TOTP(normalized_secret).now()


async def _fill_first_available(page, selectors: tuple[str, ...], value: str) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count() <= 0:
                continue
            await locator.click(timeout=1_000)
            await locator.fill(value)
            return True
        except Exception:
            continue
    return False


async def _click_first_available(page, selectors: tuple[str, ...], *, timeout_ms: int = 5_000) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count() <= 0:
                continue
            await locator.click(timeout=timeout_ms)
            return True
        except Exception:
            continue
    return False


async def _nudge_chatbox(page) -> bool:
    for selector in _CHATBOX_SELECTORS:
        try:
            locator = page.locator(selector).first
            await locator.click(timeout=1_000)
            await locator.press_sequentially("x", delay=50)
            await locator.press("Backspace")
            return True
        except Exception:
            continue
    return False


async def _advance_login_flow(
    page,
    settings: Settings,
    *,
    allow_manual_reauth: bool = False,
) -> None:
    if "login.microsoftonline.com" not in page.url:
        return

    if _credentials_available(settings):
        if await _click_first_available(page, (_USE_ANOTHER_ACCOUNT_SELECTOR,), timeout_ms=2_000):
            await page.wait_for_timeout(2_000)
            return

        if await _fill_first_available(page, (_EMAIL_SELECTOR,), settings.login_email or ""):
            if await _click_first_available(page, (_SUBMIT_SELECTOR,)):
                await page.wait_for_timeout(2_000)
                return

        if await _fill_first_available(page, (_PASSWORD_SELECTOR,), settings.login_password or ""):
            if await _click_first_available(page, (_SUBMIT_SELECTOR,)):
                await page.wait_for_timeout(2_000)
                return

    try:
        account_tiles = page.locator(_ACCOUNT_TILE_SELECTOR)
        if await account_tiles.count() > 0:
            await account_tiles.first.click(timeout=5_000)
            await page.wait_for_timeout(2_000)
            return
    except Exception:
        pass

    if "login.microsoftonline.com" not in page.url:
        return

    try:
        body_text = await page.locator("body").inner_text()
    except Exception:
        body_text = ""

    if _credentials_available(settings) and _body_mentions_totp(body_text):
        code = _generate_totp_code(settings.login_totp_secret or "")
        if await _fill_first_available(page, _TOTP_SELECTORS, code):
            if await _click_first_available(page, _TOTP_CONTINUE_SELECTORS):
                await page.wait_for_timeout(2_000)
                return

    if "stay signed in" in body_text.lower():
        if await _click_first_available(page, (_SUBMIT_SELECTOR,)):
            await page.wait_for_timeout(2_000)
            return

    if "Enter password" in body_text:
        if allow_manual_reauth:
            return
        if _credentials_available(settings):
            raise PlaywrightRefreshError(
                "Credential-based login could not advance past the Microsoft password prompt."
            )
        raise PlaywrightRefreshError(
            "Saved Playwright profile needs interactive reauthentication: "
            "Microsoft is prompting for the account password."
        )

    if _body_mentions_totp(body_text):
        if allow_manual_reauth:
            return
        raise PlaywrightRefreshError(
            "Credential-based login reached the Microsoft verification-code step, but the TOTP "
            "input could not be completed automatically."
        )

async def _launch_persistent_context(
    settings: Settings,
    *,
    headed: bool,
):
    async_playwright = _import_playwright()
    profile_dir = Path(settings.profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)

    launch_kwargs = {
        "user_data_dir": str(profile_dir),
        "headless": not headed,
    }
    if settings.browser_channel:
        launch_kwargs["channel"] = settings.browser_channel

    manager = async_playwright()
    playwright = await manager.__aenter__()
    try:
        context = await playwright.chromium.launch_persistent_context(**launch_kwargs)
    except Exception:
        await manager.__aexit__(None, None, None)
        raise
    return manager, context


async def _capture_token(
    settings: Settings,
    headed: bool,
    timeout_seconds: int,
    *,
    allow_manual_reauth: bool = False,
) -> str:
    token_event = asyncio.Event()
    token_box: dict[str, str | None] = {"value": None}

    def set_token(raw: str | None) -> None:
        token = _extract_token(raw)
        if token and not token_box["value"]:
            token_box["value"] = token
            token_event.set()

    def attach_listeners(page) -> None:
        page.on("websocket", lambda ws: set_token(getattr(ws, "url", None)))
        page.on("request", lambda request: set_token(request.url))

    try:
        manager, context = await _launch_persistent_context(settings, headed=headed)
        try:
            pages = list(context.pages)
            if not pages:
                pages = [await context.new_page()]

            for page in pages:
                attach_listeners(page)

            context.on("page", attach_listeners)
            page = pages[0]
            await page.goto(settings.login_url, wait_until="domcontentloaded")

            deadline = time.time() + timeout_seconds
            next_chatbox_nudge = time.time()
            while time.time() < deadline:
                await _advance_login_flow(
                    page,
                    settings,
                    allow_manual_reauth=allow_manual_reauth,
                )
                if token_box["value"]:
                    return token_box["value"]
                with contextlib.suppress(Exception):
                    set_token(await page.evaluate(_STORAGE_JS))
                if token_box["value"]:
                    return token_box["value"]
                if time.time() >= next_chatbox_nudge:
                    with contextlib.suppress(Exception):
                        await _nudge_chatbox(page)
                    next_chatbox_nudge = time.time() + 5
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(token_event.wait(), timeout=min(1.0, remaining))
                except asyncio.TimeoutError:
                    pass
        finally:
            await context.close()
            await manager.__aexit__(None, None, None)
    except PlaywrightRefreshError:
        raise
    except Exception as exc:
        raise PlaywrightRefreshError(f"Playwright token capture failed: {exc}") from exc

    raise PlaywrightRefreshError(
        "Timed out waiting for a Copilot access token. If this is the first run, "
        "execute `copilot-openai-proxy login` in headed mode and complete the sign-in flow."
    )


async def login_with_playwright(
    settings: Settings,
    timeout_seconds: int = 600,
    *,
    headed: bool = True,
) -> Path:
    token = await _capture_token(
        settings,
        headed=headed,
        timeout_seconds=timeout_seconds,
        allow_manual_reauth=headed,
    )
    return write_access_token(settings, token)


async def refresh_token_with_playwright(
    settings: Settings,
    *,
    headed: bool = False,
    timeout_seconds: int = 90,
) -> Path:
    token = await _capture_token(settings, headed=headed, timeout_seconds=timeout_seconds)
    return write_access_token(settings, token)


async def run_refresh_daemon(settings: Settings) -> None:
    startup_refresh_done = False
    while True:
        try:
            try:
                token = load_access_token(settings)
            except TokenStoreError:
                token = None

            if (
                not startup_refresh_done
                or token is None
                or token_needs_refresh(token, settings.token_refresh_buffer_seconds)
            ):
                path = await refresh_token_with_playwright(
                    settings,
                    timeout_seconds=settings.token_capture_timeout_seconds,
                )
                if token is None:
                    print(f"Created token at {path}.")
                else:
                    print(f"Refreshed token at {path}.")
                token = load_access_token(settings)
                startup_refresh_done = True

            delay = max(
                5,
                token_expires_at(token) - settings.token_refresh_buffer_seconds - int(time.time()),
            )
            await asyncio.sleep(delay)
        except (PlaywrightRefreshError, TokenStoreError) as exc:
            print(f"Refresh failed: {exc}")
            await asyncio.sleep(settings.token_refresh_retry_seconds)
