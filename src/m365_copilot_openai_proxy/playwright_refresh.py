from __future__ import annotations

import asyncio
import contextlib
import json
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


def _normalize_cookie(raw: object, index: int) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise PlaywrightRefreshError(f"Cookie entry {index} must be a JSON object.")

    name = raw.get("name")
    value = raw.get("value")
    if not isinstance(name, str) or not name:
        raise PlaywrightRefreshError(f"Cookie entry {index} is missing a valid name.")
    if not isinstance(value, str):
        raise PlaywrightRefreshError(f"Cookie entry {index} is missing a valid value.")

    cookie: dict[str, object] = {
        "name": name,
        "value": value,
    }

    url = raw.get("url")
    domain = raw.get("domain")
    path = raw.get("path")
    if isinstance(url, str) and url:
        cookie["url"] = url
    elif isinstance(domain, str) and domain:
        cookie["domain"] = domain
        cookie["path"] = path if isinstance(path, str) and path else "/"
    else:
        raise PlaywrightRefreshError(
            f"Cookie entry {index} must include either url or domain."
        )

    expires = raw.get("expires", raw.get("expirationDate"))
    if expires not in (None, "", -1):
        if not isinstance(expires, (int, float)):
            raise PlaywrightRefreshError(f"Cookie entry {index} has an invalid expires value.")
        cookie["expires"] = float(expires)

    for field in ("httpOnly", "secure"):
        value = raw.get(field)
        if isinstance(value, bool):
            cookie[field] = value

    same_site = raw.get("sameSite")
    if isinstance(same_site, str):
        normalized_same_site = {
            "strict": "Strict",
            "lax": "Lax",
            "none": "None",
            "no_restriction": "None",
        }.get(same_site.strip().lower())
        if normalized_same_site:
            cookie["sameSite"] = normalized_same_site

    return cookie


def load_cookies(cookies_path: str | Path) -> list[dict[str, object]]:
    path = Path(cookies_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PlaywrightRefreshError(f"Cookie file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PlaywrightRefreshError(f"Cookie file is not valid JSON: {path}") from exc

    if isinstance(payload, dict):
        payload = payload.get("cookies")

    if not isinstance(payload, list) or not payload:
        raise PlaywrightRefreshError(
            "Cookie file must contain a non-empty JSON array, or an object with a non-empty "
            "`cookies` array."
        )

    return [_normalize_cookie(cookie, index) for index, cookie in enumerate(payload, start=1)]


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


async def import_cookies_with_playwright(
    settings: Settings,
    cookies_path: str | Path,
    *,
    headed: bool = False,
) -> int:
    cookies = load_cookies(cookies_path)
    manager, context = await _launch_persistent_context(settings, headed=headed)
    try:
        await context.add_cookies(cookies)
    except Exception as exc:
        raise PlaywrightRefreshError(f"Playwright cookie import failed: {exc}") from exc
    finally:
        await context.close()
        await manager.__aexit__(None, None, None)

    return len(cookies)


async def export_cookies_with_playwright(
    settings: Settings,
    output_path: str | Path,
    *,
    headed: bool = False,
) -> tuple[Path, int]:
    path = Path(output_path)
    manager, context = await _launch_persistent_context(settings, headed=headed)
    try:
        cookies = await context.cookies()
    except Exception as exc:
        raise PlaywrightRefreshError(f"Playwright cookie export failed: {exc}") from exc
    finally:
        await context.close()
        await manager.__aexit__(None, None, None)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cookies, indent=2) + "\n", encoding="utf-8")
    return path, len(cookies)


async def _capture_token(
    settings: Settings,
    headed: bool,
    timeout_seconds: int,
    *,
    initial_cookies: list[dict[str, object]] | None = None,
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
            if initial_cookies:
                await context.add_cookies(initial_cookies)

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
    cookies_path: str | Path | None = None,
) -> Path:
    initial_cookies = load_cookies(cookies_path) if cookies_path else None
    token = await _capture_token(
        settings,
        headed=headed,
        timeout_seconds=timeout_seconds,
        initial_cookies=initial_cookies,
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
