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


async def _capture_token(settings: Settings, headed: bool, timeout_seconds: int) -> str:
    async_playwright = _import_playwright()
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

    profile_dir = Path(settings.profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)

    try:
        async with async_playwright() as playwright:
            launch_kwargs = {
                "user_data_dir": str(profile_dir),
                "headless": not headed,
            }
            if settings.browser_channel:
                launch_kwargs["channel"] = settings.browser_channel

            context = await playwright.chromium.launch_persistent_context(**launch_kwargs)
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
                while time.time() < deadline:
                    if token_box["value"]:
                        return token_box["value"]
                    with contextlib.suppress(Exception):
                        set_token(await page.evaluate(_STORAGE_JS))
                    if token_box["value"]:
                        return token_box["value"]
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        break
                    await asyncio.wait_for(token_event.wait(), timeout=min(1.0, remaining))
            except TimeoutError:
                pass
            finally:
                await context.close()
    except PlaywrightRefreshError:
        raise
    except Exception as exc:
        raise PlaywrightRefreshError(f"Playwright token capture failed: {exc}") from exc

    raise PlaywrightRefreshError(
        "Timed out waiting for a Copilot access token. If this is the first run, "
        "execute `copilot-openai-proxy login` in headed mode and complete the sign-in flow."
    )


async def login_with_playwright(settings: Settings, timeout_seconds: int = 600) -> Path:
    token = await _capture_token(settings, headed=True, timeout_seconds=timeout_seconds)
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
    while True:
        try:
            try:
                token = load_access_token(settings)
            except TokenStoreError:
                path = await refresh_token_with_playwright(settings)
                print(f"Created token at {path}.")
                token = load_access_token(settings)

            if token_needs_refresh(token, settings.token_refresh_buffer_seconds):
                path = await refresh_token_with_playwright(settings)
                print(f"Refreshed token at {path}.")
                token = load_access_token(settings)

            delay = max(
                5,
                token_expires_at(token) - settings.token_refresh_buffer_seconds - int(time.time()),
            )
            await asyncio.sleep(delay)
        except (PlaywrightRefreshError, TokenStoreError) as exc:
            print(f"Refresh failed: {exc}")
            await asyncio.sleep(settings.token_refresh_retry_seconds)
