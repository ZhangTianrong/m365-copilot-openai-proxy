from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .config import Settings
from .models import AuthSessionSnapshot


class TokenStoreError(RuntimeError):
    pass


def decode_jwt_payload(token: str) -> dict:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def _ensure_oid_tid_from_path(path: str) -> tuple[str, str]:
    tail = path.rstrip("/").rsplit("/", 1)[-1]
    if "@" not in tail:
        raise TokenStoreError("WebSocket URL is missing the Copilot oid@tid path segment.")
    oid, tid = tail.split("@", 1)
    if not oid or not tid:
        raise TokenStoreError("WebSocket URL contains an invalid Copilot oid@tid path segment.")
    return oid, tid


def auth_state_path(settings: Settings) -> Path:
    return Path(settings.auth_state_file)


def access_token_path(settings: Settings) -> Path:
    return Path(settings.access_token_file)


def parse_websocket_url(raw: str) -> AuthSessionSnapshot:
    parts = urlsplit(raw.strip())
    query = parse_qs(parts.query)
    token_values = query.get("access_token")
    if not token_values or not token_values[0]:
        raise TokenStoreError("WebSocket URL is missing an access_token query parameter.")
    oid, tid = _ensure_oid_tid_from_path(parts.path)
    return AuthSessionSnapshot(
        account_mode="personal",
        access_token=token_values[0],
        expires_at=None,
        captured_at=int(time.time()),
        oid=oid,
        tid=tid,
        websocket_url=raw.strip(),
    )


def build_enterprise_auth_session(
    token: str,
    *,
    captured_at: int | None = None,
    websocket_url: str | None = None,
) -> AuthSessionSnapshot:
    claims = decode_jwt_payload(token)
    exp = claims.get("exp")
    if not isinstance(exp, int):
        raise TokenStoreError("Token is missing a valid exp claim.")
    oid = claims.get("oid")
    tid = claims.get("tid")
    if not isinstance(oid, str) or not oid or not isinstance(tid, str) or not tid:
        raise TokenStoreError("Enterprise access token is missing oid or tid claims.")
    return AuthSessionSnapshot(
        account_mode="enterprise",
        access_token=token.strip(),
        expires_at=exp,
        captured_at=captured_at or int(time.time()),
        oid=oid,
        tid=tid,
        websocket_url=websocket_url,
    )


def token_expires_at(token: str) -> int:
    claims = decode_jwt_payload(token)
    exp = claims.get("exp")
    if not isinstance(exp, int):
        raise TokenStoreError("Token is missing a valid exp claim.")
    return exp


def token_is_expired(token: str) -> bool:
    return time.time() >= token_expires_at(token)


def token_needs_refresh(token: str, buffer_seconds: int) -> bool:
    return time.time() >= token_expires_at(token) - buffer_seconds


def auth_session_expires_at(auth_session: AuthSessionSnapshot) -> int | None:
    if auth_session.account_mode == "personal":
        expirations = [
            value
            for value in (
                auth_session.graph_expires_at,
                auth_session.search_expires_at,
            )
            if value is not None
        ]
        if expirations:
            return min(expirations)
    expirations = [
        value
        for value in (
            auth_session.expires_at,
            auth_session.graph_expires_at,
            auth_session.search_expires_at,
        )
        if value is not None
    ]
    if not expirations:
        return None
    return min(expirations)


def auth_session_needs_refresh(auth_session: AuthSessionSnapshot, buffer_seconds: int) -> bool:
    expires_at = auth_session_expires_at(auth_session)
    if expires_at is None:
        return True
    return time.time() >= expires_at - buffer_seconds


def _validate_loaded_auth_session(settings: Settings, auth_session: AuthSessionSnapshot) -> AuthSessionSnapshot:
    if auth_session.account_mode != settings.account_mode:
        raise TokenStoreError(
            f"Auth session account mode mismatch: configured {settings.account_mode}, "
            f"but saved auth state is {auth_session.account_mode}. Re-run "
            "`copilot-openai-proxy login` or fix M365_ACCOUNT_MODE."
        )
    return auth_session


def load_auth_session(settings: Settings) -> AuthSessionSnapshot:
    state_path = auth_state_path(settings)
    if state_path.exists():
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            return _validate_loaded_auth_session(
                settings,
                AuthSessionSnapshot.model_validate(payload),
            )
        except Exception as exc:
            raise TokenStoreError(f"Cannot load auth state from {state_path}: {exc}") from exc

    if settings.account_mode == "enterprise":
        token_path = access_token_path(settings)
        if token_path.exists():
            token = token_path.read_text(encoding="utf-8").strip()
            if token:
                return build_enterprise_auth_session(token)
        if settings.access_token:
            return build_enterprise_auth_session(settings.access_token.strip())
        raise TokenStoreError(
            "No access token is configured. Set M365_ACCESS_TOKEN, or run "
            "`copilot-openai-proxy login` / `copilot-openai-proxy refresh-token` "
            f"to populate {token_path}."
        )

    raise TokenStoreError(
        "No personal auth session is configured. Run `copilot-openai-proxy login`, "
        "`copilot-openai-proxy refresh-token`, or `copilot-openai-proxy set-token` "
        "with a full Copilot WebSocket URL."
    )


def load_access_token(settings: Settings) -> str:
    return load_auth_session(settings).access_token


def write_auth_session(settings: Settings, auth_session: AuthSessionSnapshot) -> Path:
    state_path = auth_state_path(settings)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        auth_session.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )

    token_path = access_token_path(settings)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(auth_session.access_token.strip() + "\n", encoding="utf-8")
    return state_path


def write_access_token(settings: Settings, token: str) -> Path:
    if settings.account_mode != "enterprise":
        raise TokenStoreError(
            "Raw token writes are only supported in enterprise mode. "
            "For personal mode, provide a full Copilot WebSocket URL."
        )
    auth_session = build_enterprise_auth_session(token.strip())
    write_auth_session(settings, auth_session)
    return access_token_path(settings)
