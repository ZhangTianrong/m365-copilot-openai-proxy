from __future__ import annotations

import base64
import json
import time
from pathlib import Path

from .config import Settings


class TokenStoreError(RuntimeError):
    pass


def decode_jwt_payload(token: str) -> dict:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


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


def access_token_path(settings: Settings) -> Path:
    return Path(settings.access_token_file)


def load_access_token(settings: Settings) -> str:
    path = access_token_path(settings)
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token

    if settings.access_token:
        return settings.access_token.strip()

    raise TokenStoreError(
        "No access token is configured. Set M365_ACCESS_TOKEN, or run "
        "`copilot-openai-proxy login` / `copilot-openai-proxy refresh-token` "
        f"to populate {path}."
    )


def write_access_token(settings: Settings, token: str) -> Path:
    path = access_token_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token.strip() + "\n", encoding="utf-8")
    return path
