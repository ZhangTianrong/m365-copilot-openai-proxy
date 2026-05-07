from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .config import Settings
from .models import AuthSessionSnapshot
from .token_store import (
    TokenStoreError,
    auth_session_expires_at,
    auth_session_needs_refresh,
    build_enterprise_auth_session,
    load_auth_session,
    parse_websocket_url,
    write_auth_session,
)

_STORAGE_JS = """
(() => {
    const preferredTargets = ["m365copilot", "mdcpp", "substrate", "officehome.all"];
    const records = [];
    const stores = [["sessionStorage", sessionStorage], ["localStorage", localStorage]];
    for (const [storeName, store] of stores) {
        for (const key of Object.keys(store)) {
            if (!key.includes("accesstoken")) continue;
            try {
                const value = JSON.parse(store.getItem(key));
                if (value && value.secret && value.secret.startsWith("eyJ")) {
                    records.push({
                        store: storeName,
                        key,
                        secret: value.secret,
                        target: value.target || "",
                        expiresOn: value.expiresOn || value.expires_on || value.expiration || value.expiresAt || null,
                    });
                }
            } catch {}
        }
    }
    for (const needle of preferredTargets) {
        const match = records.find((record) => record.target.toLowerCase().includes(needle));
        if (match) return match;
    }
    return records[0] || null;
})()
"""

_GRAPH_TOKEN_JS = """
async ({ scope, clientId }) => {
    const service = window.nestedAppAuthService;
    const resolvedClientId = clientId || service?.authOptions?.aadAppId || window.msal?.clientIds?.[0];
    if (!service || !resolvedClientId || typeof service.handleRequest !== "function") {
        return null;
    }
    try {
        const result = await service.handleRequest(
            {
                requestId: "codex-graph-token",
                method: "GetToken",
                tokenParams: {
                    clientId: resolvedClientId,
                    scope,
                    correlationId: "codex-graph-token",
                    forceRefresh: false,
                },
            },
            new URL(window.location.href),
        );
        const token = result?.token?.access_token;
        const expiresIn = result?.token?.expires_in;
        if (typeof token !== "string" || !token) {
            return null;
        }
        return {
            access_token: token,
            expires_in: typeof expiresIn === "number" ? expiresIn : Number(expiresIn || 0),
        };
    } catch {
        return null;
    }
}
"""

_PERSONAL_UPLOAD_CLIENT_ID = "c0ab8ce9-e9a0-42e7-b064-33d422df41f1"

_PERSONAL_SUBSTRATE_TOKEN_JS = """
(() => {
    const preferredTargets = [
        "https://substrate.office.com/.default https://substrate.office.com/m365.access",
        "https://substrate.office.com/.default",
    ];
    const stores = [["sessionStorage", sessionStorage], ["localStorage", localStorage]];
    const records = [];
    for (const [storeName, store] of stores) {
        for (const key of Object.keys(store)) {
            if (!key.includes("accesstoken")) continue;
            try {
                const value = JSON.parse(store.getItem(key));
                const secret = value?.secret;
                const target = (value?.target || "").toLowerCase();
                if (typeof secret !== "string" || !secret || !target) continue;
                records.push({
                    store: storeName,
                    key,
                    secret,
                    target,
                    expiresOn: value.expiresOn || value.expires_on || value.expiration || value.expiresAt || null,
                });
            } catch {}
        }
    }
    for (const needle of preferredTargets) {
        const match = records.find((record) => record.target.includes(needle));
        if (match) return match;
    }
    return records.find((record) => record.target.includes("substrate.office.com")) || null;
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
_PERSONAL_PASSWORD_SELECTORS = (
    'input[type="password"]',
    'input[name="passwd"]',
    "#i0118",
)
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


@dataclass(slots=True)
class AuthenticatedSessionState:
    saw_login_host: bool = False
    detected_account_mode: str | None = None
    saw_enterprise_login_host: bool = False
    saw_personal_login_host: bool = False


def _verbose_log(settings: Settings, event: str, **fields: object) -> None:
    if not settings.debug_logging:
        return
    payload = {"component": "playwright_refresh", "event": event, **fields}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _validate_detected_account_mode(
    settings: Settings,
    auth_state: AuthenticatedSessionState,
) -> None:
    if settings.account_mode == "enterprise" and auth_state.saw_personal_login_host:
        raise PlaywrightRefreshError(
            "M365_ACCOUNT_MODE is enterprise, but the browser session entered the "
            "personal Microsoft sign-in flow."
        )
    if settings.account_mode == "personal" and auth_state.saw_enterprise_login_host:
        raise PlaywrightRefreshError(
            "M365_ACCOUNT_MODE is personal, but the browser session entered the "
            "enterprise Microsoft sign-in flow."
        )


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


def _parse_storage_auth_record(raw: str | dict | None) -> dict[str, object] | None:
    if raw is None:
        return None
    value = raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, dict):
        return None
    secret = value.get("secret")
    if not isinstance(secret, str) or not secret:
        return None
    record: dict[str, object] = {"access_token": secret}
    expires_on = value.get("expiresOn")
    if isinstance(expires_on, str) and expires_on.isdigit():
        record["expires_at"] = int(expires_on)
    elif isinstance(expires_on, (int, float)):
        record["expires_at"] = int(expires_on)
    target = value.get("target")
    if isinstance(target, str):
        record["target"] = target
    return record


def _infer_account_mode_from_url(url: str) -> str | None:
    if "login.live.com" in url:
        return "personal"
    if "login.microsoftonline.com" in url:
        return "enterprise"
    return None


def _build_auth_snapshot(
    settings: Settings,
    auth_data: dict[str, object],
) -> AuthSessionSnapshot | None:
    token = auth_data.get("access_token")
    if not isinstance(token, str) or not token:
        return None
    captured_at = int(time.time())
    expires_at = auth_data.get("expires_at")
    websocket_url = auth_data.get("websocket_url")
    if settings.account_mode == "personal":
        if not isinstance(websocket_url, str) or not websocket_url:
            return None
        if not isinstance(expires_at, int):
            return None
        snapshot = parse_websocket_url(websocket_url)
        snapshot.access_token = token
        snapshot.expires_at = expires_at
        snapshot.captured_at = captured_at
        graph_token = auth_data.get("graph_access_token")
        graph_expires_at = auth_data.get("graph_expires_at")
        if isinstance(graph_token, str) and graph_token:
            snapshot.graph_access_token = graph_token
        if isinstance(graph_expires_at, int):
            snapshot.graph_expires_at = graph_expires_at
        search_token = auth_data.get("search_access_token")
        search_expires_at = auth_data.get("search_expires_at")
        if isinstance(search_token, str) and search_token:
            snapshot.search_access_token = search_token
        if isinstance(search_expires_at, int):
            snapshot.search_expires_at = search_expires_at
        return snapshot
    try:
        snapshot = build_enterprise_auth_session(
            token,
            captured_at=captured_at,
            websocket_url=websocket_url if isinstance(websocket_url, str) else None,
        )
    except TokenStoreError:
        return None
    graph_token = auth_data.get("graph_access_token")
    graph_expires_at = auth_data.get("graph_expires_at")
    if isinstance(graph_token, str) and graph_token:
        snapshot.graph_access_token = graph_token
    if isinstance(graph_expires_at, int):
        snapshot.graph_expires_at = graph_expires_at
    search_token = auth_data.get("search_access_token")
    search_expires_at = auth_data.get("search_expires_at")
    if isinstance(search_token, str) and search_token:
        snapshot.search_access_token = search_token
    if isinstance(search_expires_at, int):
        snapshot.search_expires_at = search_expires_at
    return snapshot


def _should_accept_storage_record(settings: Settings, record: dict[str, object]) -> bool:
    if settings.account_mode != "enterprise":
        return True
    token = record.get("access_token")
    if not isinstance(token, str) or not token:
        return False
    try:
        build_enterprise_auth_session(token)
    except TokenStoreError:
        return False
    return True


async def _capture_graph_access_token(page, *, client_id: str | None = None) -> dict[str, object] | None:
    try:
        result = await page.evaluate(
            _GRAPH_TOKEN_JS,
            {"scope": "https://graph.microsoft.com/.default", "clientId": client_id},
        )
    except Exception:
        return None
    if not isinstance(result, dict):
        return None
    token = result.get("access_token")
    expires_in = result.get("expires_in")
    if not isinstance(token, str) or not token:
        return None
    captured: dict[str, object] = {"graph_access_token": token}
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        captured["graph_expires_at"] = int(time.time() + expires_in)
    return captured


async def _capture_search_access_token(page) -> dict[str, object] | None:
    return await _capture_search_access_token_for_scope(
        page,
        "https://substrate.office.com/search/.default",
    )


async def _capture_search_access_token_for_scope(
    page,
    scope: str,
    *,
    client_id: str | None = None,
) -> dict[str, object] | None:
    try:
        result = await page.evaluate(
            _GRAPH_TOKEN_JS,
            {"scope": scope, "clientId": client_id},
        )
    except Exception:
        return None
    if not isinstance(result, dict):
        return None
    token = result.get("access_token")
    expires_in = result.get("expires_in")
    if not isinstance(token, str) or not token:
        return None
    captured: dict[str, object] = {"search_access_token": token}
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        captured["search_expires_at"] = int(time.time() + expires_in)
    return captured


async def _capture_personal_substrate_access_token(page) -> dict[str, object] | None:
    try:
        result = await page.evaluate(_PERSONAL_SUBSTRATE_TOKEN_JS)
    except Exception:
        return None
    record = _parse_storage_auth_record(result)
    if not record:
        return None
    return record


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


async def _click_first_visible_text(page, texts: tuple[str, ...], *, exact: bool = True) -> bool:
    for text in texts:
        try:
            locator = page.get_by_text(text, exact=exact).first
            if await locator.count() <= 0:
                continue
            await locator.click(timeout=5_000)
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
    url = page.url
    is_enterprise_login = "login.microsoftonline.com" in url
    is_personal_login = "login.live.com" in url
    if not is_enterprise_login and not is_personal_login:
        return

    try:
        body_text = await page.locator("body").inner_text()
    except Exception:
        body_text = ""
    lower_body = body_text.lower()

    if is_enterprise_login:
        if _credentials_available(settings):
            if await _click_first_available(page, (_USE_ANOTHER_ACCOUNT_SELECTOR,), timeout_ms=2_000):
                _verbose_log(settings, "login.action", action="enterprise.use_another_account")
                await page.wait_for_timeout(2_000)
                return

            if await _fill_first_available(page, (_EMAIL_SELECTOR,), settings.login_email or ""):
                if await _click_first_available(page, (_SUBMIT_SELECTOR,)):
                    _verbose_log(settings, "login.action", action="enterprise.submit_email")
                    await page.wait_for_timeout(2_000)
                    return

            if await _fill_first_available(page, (_PASSWORD_SELECTOR,), settings.login_password or ""):
                if await _click_first_available(page, (_SUBMIT_SELECTOR,)):
                    _verbose_log(settings, "login.action", action="enterprise.submit_password")
                    await page.wait_for_timeout(2_000)
                    return

        try:
            account_tiles = page.locator(_ACCOUNT_TILE_SELECTOR)
            if await account_tiles.count() > 0:
                await account_tiles.first.click(timeout=5_000)
                _verbose_log(settings, "login.action", action="enterprise.select_account_tile")
                await page.wait_for_timeout(2_000)
                return
        except Exception:
            pass

        if _credentials_available(settings) and _body_mentions_totp(body_text):
            code = _generate_totp_code(settings.login_totp_secret or "")
            if await _fill_first_available(page, _TOTP_SELECTORS, code):
                if await _click_first_available(page, _TOTP_CONTINUE_SELECTORS):
                    _verbose_log(settings, "login.action", action="enterprise.submit_totp")
                    await page.wait_for_timeout(2_000)
                    return

        if "stay signed in" in lower_body:
            if await _click_first_available(page, (_SUBMIT_SELECTOR,)):
                _verbose_log(settings, "login.action", action="enterprise.confirm_stay_signed_in")
                await page.wait_for_timeout(2_000)
                return

        if "enter password" in lower_body:
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
        return

    if _credentials_available(settings):
        if await _fill_first_available(page, (_EMAIL_SELECTOR, 'input[type="email"]'), settings.login_email or ""):
            if await _click_first_available(page, (_SUBMIT_SELECTOR, 'input[type="submit"]', 'button[type="submit"]')):
                _verbose_log(settings, "login.action", action="personal.submit_email")
                await page.wait_for_timeout(2_000)
                return

        if await _fill_first_available(page, _PERSONAL_PASSWORD_SELECTORS, settings.login_password or ""):
            if await _click_first_available(page, ('button[type="submit"]', 'input[type="submit"]', _SUBMIT_SELECTOR)):
                _verbose_log(settings, "login.action", action="personal.submit_password")
                await page.wait_for_timeout(2_000)
                return

        if "other ways to sign in" in lower_body:
            if await _click_first_visible_text(page, ("Other ways to sign in",)):
                _verbose_log(settings, "login.action", action="personal.choose_other_sign_in_method")
                await page.wait_for_timeout(2_000)
                return

        if "use your password" in lower_body:
            if await _click_first_visible_text(page, ("Use your password",)):
                _verbose_log(settings, "login.action", action="personal.choose_password_sign_in")
                await page.wait_for_timeout(2_000)
                return

    if "stay signed in" in lower_body:
        if await _click_first_visible_text(page, ("Yes", "No")):
            _verbose_log(settings, "login.action", action="personal.confirm_stay_signed_in")
            await page.wait_for_timeout(2_000)
            return

    if "use your password" in lower_body or "enter your password" in lower_body:
        if allow_manual_reauth:
            return
        raise PlaywrightRefreshError(
            "Saved Playwright profile needs interactive reauthentication: "
            "Microsoft consumer sign-in is prompting for the account password."
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
    manager = None
    context = None
    try:
        manager, context, _page, auth_session, _auth_state = await open_authenticated_context(
            settings,
            headed=headed,
            timeout_seconds=timeout_seconds,
            allow_manual_reauth=allow_manual_reauth,
        )
        return auth_session.access_token
    finally:
        if context is not None:
            await context.close()
        if manager is not None:
            await manager.__aexit__(None, None, None)


async def open_authenticated_context(
    settings: Settings,
    *,
    headed: bool,
    timeout_seconds: int,
    allow_manual_reauth: bool = False,
):
    token_event = asyncio.Event()
    auth_data: dict[str, object] = {}
    auth_state = AuthenticatedSessionState()
    logged_account_mode: str | None = None
    token_sources_logged: set[str] = set()
    next_wait_log = time.time()

    def record_websocket(raw: str | None) -> None:
        if not raw or "/m365Copilot/Chathub/" not in raw:
            return
        try:
            parsed = parse_websocket_url(raw)
        except TokenStoreError:
            return
        auth_data["access_token"] = parsed.access_token
        auth_data["websocket_url"] = parsed.websocket_url
        auth_data["oid"] = parsed.oid
        auth_data["tid"] = parsed.tid
        if "websocket_url" not in token_sources_logged:
            token_sources_logged.add("websocket_url")
            _verbose_log(
                settings,
                "token.captured",
                source="websocket_url",
                account_mode=settings.account_mode,
            )
        token_event.set()

    def set_token(raw: str | None) -> None:
        record_websocket(raw)
        token = _extract_token(raw)
        if token:
            auth_data["access_token"] = token
            if "access_token" not in token_sources_logged:
                token_sources_logged.add("access_token")
                _verbose_log(
                    settings,
                    "token.captured",
                    source="request_or_websocket",
                    account_mode=settings.account_mode,
                )
            token_event.set()

    def update_from_storage(raw: str | dict | None) -> None:
        record = _parse_storage_auth_record(raw)
        if not record:
            return
        if not _should_accept_storage_record(settings, record):
            return
        auth_data.update(record)
        if "storage" not in token_sources_logged:
            token_sources_logged.add("storage")
            _verbose_log(
                settings,
                "token.captured",
                source="browser_storage",
                account_mode=settings.account_mode,
            )
        token_event.set()

    def current_auth_snapshot() -> AuthSessionSnapshot | None:
        return _build_auth_snapshot(settings, auth_data)

    def attach_listeners(page) -> None:
        page.on("websocket", lambda ws: set_token(getattr(ws, "url", None)))
        page.on("request", lambda request: set_token(request.url))

    try:
        _verbose_log(
            settings,
            "auth.start",
            headed=headed,
            timeout_seconds=timeout_seconds,
            login_url=settings.login_url,
            profile_dir=settings.profile_dir,
            allow_manual_reauth=allow_manual_reauth,
            configured_account_mode=settings.account_mode,
        )
        manager, context = await _launch_persistent_context(settings, headed=headed)
        pages = list(context.pages)
        if not pages:
            pages = [await context.new_page()]

        for page in pages:
            attach_listeners(page)

        context.on("page", attach_listeners)
        page = pages[0]
        await page.goto(settings.login_url, wait_until="domcontentloaded")
        _verbose_log(settings, "auth.page_ready", url=page.url)

        deadline = time.time() + timeout_seconds
        next_chatbox_nudge = time.time()
        while time.time() < deadline:
            detected_mode = _infer_account_mode_from_url(page.url)
            if detected_mode is not None:
                auth_state.saw_login_host = True
                if detected_mode == "enterprise":
                    auth_state.saw_enterprise_login_host = True
                    auth_state.detected_account_mode = auth_state.detected_account_mode or "enterprise"
                else:
                    auth_state.saw_personal_login_host = True
                    auth_state.detected_account_mode = "personal"
                if detected_mode != logged_account_mode:
                    logged_account_mode = detected_mode
                    _verbose_log(
                        settings,
                        "login.flow_detected",
                        detected_account_mode=detected_mode,
                        url=page.url,
                    )
            await _advance_login_flow(
                page,
                settings,
                allow_manual_reauth=allow_manual_reauth,
            )
            _validate_detected_account_mode(settings, auth_state)
            snapshot = current_auth_snapshot()
            if snapshot is not None:
                if settings.account_mode == "personal":
                    substrate_auth = await _capture_personal_substrate_access_token(page)
                    if substrate_auth:
                        auth_data.update(substrate_auth)
                        snapshot = current_auth_snapshot() or snapshot
                graph_auth = await _capture_graph_access_token(
                    page,
                    client_id=(
                        _PERSONAL_UPLOAD_CLIENT_ID
                        if settings.account_mode == "personal"
                        else None
                    ),
                )
                if graph_auth:
                    auth_data.update(graph_auth)
                    _verbose_log(settings, "token.captured", source="graph_access_token")
                    snapshot = current_auth_snapshot() or snapshot
                if settings.account_mode == "personal":
                    search_auth = await _capture_search_access_token_for_scope(
                        page,
                        "https://substrate.office.com/.default",
                        client_id=_PERSONAL_UPLOAD_CLIENT_ID,
                    )
                else:
                    search_auth = await _capture_search_access_token(page)
                if search_auth:
                    auth_data.update(search_auth)
                    _verbose_log(settings, "token.captured", source="search_access_token")
                    snapshot = current_auth_snapshot() or snapshot
                _validate_detected_account_mode(settings, auth_state)
                _verbose_log(
                    settings,
                    "auth.ready",
                    account_mode=snapshot.account_mode,
                    expires_at=snapshot.expires_at,
                    effective_expires_at=auth_session_expires_at(snapshot),
                    has_websocket_url=bool(snapshot.websocket_url),
                    has_graph_token=bool(snapshot.graph_access_token),
                    has_search_token=bool(snapshot.search_access_token),
                )
                return manager, context, page, snapshot, auth_state
            with contextlib.suppress(Exception):
                update_from_storage(await page.evaluate(_STORAGE_JS))
            snapshot = current_auth_snapshot()
            if snapshot is not None:
                if settings.account_mode == "personal":
                    substrate_auth = await _capture_personal_substrate_access_token(page)
                    if substrate_auth:
                        auth_data.update(substrate_auth)
                        _verbose_log(settings, "token.captured", source="personal_storage_token")
                        snapshot = current_auth_snapshot() or snapshot
                graph_auth = await _capture_graph_access_token(
                    page,
                    client_id=(
                        _PERSONAL_UPLOAD_CLIENT_ID
                        if settings.account_mode == "personal"
                        else None
                    ),
                )
                if graph_auth:
                    auth_data.update(graph_auth)
                    _verbose_log(settings, "token.captured", source="graph_access_token")
                    snapshot = current_auth_snapshot() or snapshot
                if settings.account_mode == "personal":
                    search_auth = await _capture_search_access_token_for_scope(
                        page,
                        "https://substrate.office.com/.default",
                        client_id=_PERSONAL_UPLOAD_CLIENT_ID,
                    )
                else:
                    search_auth = await _capture_search_access_token(page)
                if search_auth:
                    auth_data.update(search_auth)
                    _verbose_log(settings, "token.captured", source="search_access_token")
                    snapshot = current_auth_snapshot() or snapshot
                _validate_detected_account_mode(settings, auth_state)
                _verbose_log(
                    settings,
                    "auth.ready",
                    account_mode=snapshot.account_mode,
                    expires_at=snapshot.expires_at,
                    effective_expires_at=auth_session_expires_at(snapshot),
                    has_websocket_url=bool(snapshot.websocket_url),
                    has_graph_token=bool(snapshot.graph_access_token),
                    has_search_token=bool(snapshot.search_access_token),
                )
                return manager, context, page, snapshot, auth_state
            if time.time() >= next_chatbox_nudge:
                with contextlib.suppress(Exception):
                    nudged = await _nudge_chatbox(page)
                    if nudged:
                        _verbose_log(settings, "auth.nudge_chatbox", url=page.url)
                next_chatbox_nudge = time.time() + 5
            if time.time() >= next_wait_log:
                _verbose_log(
                    settings,
                    "auth.waiting",
                    remaining_seconds=max(0, int(deadline - time.time())),
                    url=page.url,
                )
                next_wait_log = time.time() + 10
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(token_event.wait(), timeout=min(1.0, remaining))
            except asyncio.TimeoutError:
                pass
    except PlaywrightRefreshError:
        raise
    except Exception as exc:
        raise PlaywrightRefreshError(f"Playwright token capture failed: {exc}") from exc
    finally:
        if current_auth_snapshot() is None:
            with contextlib.suppress(Exception):
                if context is not None:
                    await context.close()
            with contextlib.suppress(Exception):
                if manager is not None:
                    await manager.__aexit__(None, None, None)

    raise PlaywrightRefreshError(
        "Timed out waiting for a Copilot access token. If this is the first run, "
        "execute `copilot-openai-proxy login` in headed mode and complete the sign-in flow."
    )


async def _maybe_auto_probe_models(
    settings: Settings,
    *,
    context,
    auth_state: AuthenticatedSessionState,
) -> None:
    if not settings.auto_probe_models_on_login:
        return
    if not auth_state.saw_login_host:
        return
    try:
        from .playwright_model_probe import maybe_auto_probe_models_in_context

        await maybe_auto_probe_models_in_context(context=context, settings=settings)
    except Exception as exc:
        print(f"Model probe failed after login: {exc}")


async def login_with_playwright(
    settings: Settings,
    timeout_seconds: int = 600,
    *,
    headed: bool = True,
) -> Path:
    manager = None
    context = None
    try:
        _verbose_log(settings, "login.begin", headed=headed, timeout_seconds=timeout_seconds)
        manager, context, _page, auth_session, auth_state = await open_authenticated_context(
            settings,
            headed=headed,
            timeout_seconds=timeout_seconds,
            allow_manual_reauth=headed,
        )
        path = write_auth_session(settings, auth_session)
        _verbose_log(settings, "login.saved", path=str(path), expires_at=auth_session.expires_at)
        await _maybe_auto_probe_models(settings, context=context, auth_state=auth_state)
        return path
    finally:
        if context is not None:
            await context.close()
        if manager is not None:
            await manager.__aexit__(None, None, None)


async def refresh_token_with_playwright(
    settings: Settings,
    *,
    headed: bool = False,
    timeout_seconds: int = 90,
) -> Path:
    manager = None
    context = None
    try:
        _verbose_log(settings, "refresh.begin", headed=headed, timeout_seconds=timeout_seconds)
        manager, context, _page, auth_session, auth_state = await open_authenticated_context(
            settings,
            headed=headed,
            timeout_seconds=timeout_seconds,
        )
        path = write_auth_session(settings, auth_session)
        _verbose_log(settings, "refresh.saved", path=str(path), expires_at=auth_session.expires_at)
        await _maybe_auto_probe_models(settings, context=context, auth_state=auth_state)
        return path
    finally:
        if context is not None:
            await context.close()
        if manager is not None:
            await manager.__aexit__(None, None, None)


async def run_refresh_daemon(settings: Settings) -> None:
    startup_refresh_done = False
    while True:
        try:
            try:
                auth_session = load_auth_session(settings)
            except TokenStoreError:
                auth_session = None
                _verbose_log(settings, "daemon.auth_state_missing")
            else:
                _verbose_log(
                    settings,
                    "daemon.auth_state_loaded",
                    account_mode=auth_session.account_mode,
                    expires_at=auth_session_expires_at(auth_session),
                    captured_at=auth_session.captured_at,
                )

            needs_refresh = (
                not startup_refresh_done
                or auth_session is None
                or auth_session_needs_refresh(auth_session, settings.token_refresh_buffer_seconds)
            )
            _verbose_log(
                settings,
                "daemon.refresh_decision",
                startup_refresh_done=startup_refresh_done,
                needs_refresh=needs_refresh,
                buffer_seconds=settings.token_refresh_buffer_seconds,
            )
            if needs_refresh:
                _verbose_log(
                    settings,
                    "daemon.refresh_start",
                    timeout_seconds=settings.token_capture_timeout_seconds,
                )
                path = await refresh_token_with_playwright(
                    settings,
                    timeout_seconds=settings.token_capture_timeout_seconds,
                )
                if auth_session is None:
                    print(f"Created token at {path}.")
                else:
                    print(f"Refreshed token at {path}.")
                auth_session = load_auth_session(settings)

            expires_at = auth_session_expires_at(auth_session)
            delay = max(
                5,
                (expires_at or int(time.time()))
                - settings.token_refresh_buffer_seconds
                - int(time.time()),
            )
            startup_refresh_done = True
            _verbose_log(
                settings,
                "daemon.sleep",
                delay_seconds=delay,
                expires_at=expires_at,
                retry_seconds=settings.token_refresh_retry_seconds,
            )
            await asyncio.sleep(delay)
        except (PlaywrightRefreshError, TokenStoreError) as exc:
            print(f"Refresh failed: {exc}")
            _verbose_log(
                settings,
                "daemon.retry_after_failure",
                delay_seconds=settings.token_refresh_retry_seconds,
                error=str(exc),
            )
            await asyncio.sleep(settings.token_refresh_retry_seconds)
