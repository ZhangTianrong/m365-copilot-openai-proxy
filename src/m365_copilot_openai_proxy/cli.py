from __future__ import annotations

import argparse
import asyncio
import re
import subprocess
import sys

import uvicorn

from .app import create_app
from .config import Settings
from .playwright_model_probe import run_probe
from .playwright_refresh import (
    PlaywrightRefreshError,
    login_with_playwright,
    refresh_token_with_playwright,
    run_refresh_daemon,
)
from .token_store import TokenStoreError, parse_websocket_url, write_access_token, write_auth_session


def _extract_token(raw: str) -> str | None:
    match = re.search(r"access_token=([^&\s]+)", raw)
    token = match.group(1) if match else raw
    token = token.strip()
    return token if token.startswith("eyJ") else None


def main() -> None:
    parser = argparse.ArgumentParser(prog="copilot-openai-proxy")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("set-token").set_defaults(func=set_token_command)

    login_parser = subparsers.add_parser("login")
    login_parser.add_argument("--headless", action="store_true")
    login_parser.add_argument("--timeout", type=int, default=600)
    login_parser.set_defaults(func=login_command)

    refresh_parser = subparsers.add_parser("refresh-token")
    refresh_parser.add_argument("--headed", action="store_true")
    refresh_parser.add_argument("--timeout", type=int, default=90)
    refresh_parser.set_defaults(func=refresh_token_command)

    probe_parser = subparsers.add_parser("probe-models")
    probe_parser.add_argument("--headed", action="store_true")
    probe_parser.add_argument("--timeout", type=int, default=300)
    probe_parser.add_argument("--target-model")
    probe_parser.add_argument("--login-url", default="https://m365.cloud.microsoft/chat")
    probe_parser.set_defaults(func=probe_models_command)

    subparsers.add_parser("refresh-daemon").set_defaults(func=refresh_daemon_command)
    subparsers.add_parser("launch-edge").set_defaults(func=launch_edge_command)

    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)
    serve_parser.set_defaults(func=serve_command)

    args = parser.parse_args()
    args.func(args)


def set_token_command(_args: argparse.Namespace) -> None:
    settings = Settings()
    if settings.account_mode == "personal":
        print("Paste the full Copilot WebSocket URL, then press Enter:")
    else:
        print("Paste the full WebSocket URL (or just the access_token value), then press Enter:")
    raw = input().strip()
    try:
        if settings.account_mode == "personal":
            if "://" not in raw or "access_token=" not in raw:
                raise TokenStoreError(
                    "Personal mode requires a full Copilot WebSocket URL, not a bare token."
                )
            path = write_auth_session(settings, parse_websocket_url(raw))
        else:
            token = _extract_token(raw)
            if not token:
                print("Error: could not find a valid token. Make sure you copied the full WebSocket URL.")
                raise SystemExit(1)
            path = write_access_token(settings, token)
    except TokenStoreError as exc:
        print(f"Error: {exc}")
        raise SystemExit(1) from exc
    print(f"Token saved to {path}.")


def login_command(args: argparse.Namespace) -> None:
    settings = Settings()
    if (
        args.headless
        and settings.login_email
        and settings.login_password
        and settings.login_totp_secret
    ):
        print(
            "Launching a persistent Playwright browser profile in headless mode and attempting "
            "Microsoft 365 sign-in with the configured credential and TOTP environment variables."
        )
    elif args.headless:
        print(
            "Launching a persistent Playwright browser profile in headless mode and waiting for "
            "the existing browser profile or session to yield a Copilot token."
        )
    else:
        print(
            "Launching a persistent Playwright browser profile. Complete the Microsoft 365 sign-in flow "
            "and leave the Copilot page open until the command exits."
        )
    try:
        path = asyncio.run(
            login_with_playwright(
                settings,
                timeout_seconds=args.timeout,
                headed=not args.headless,
            )
        )
    except PlaywrightRefreshError as exc:
        print(f"Login failed: {exc}")
        raise SystemExit(1) from exc
    print(f"Token saved to {path}.")


def refresh_token_command(args: argparse.Namespace) -> None:
    settings = Settings()
    try:
        path = asyncio.run(
            refresh_token_with_playwright(
                settings,
                headed=args.headed,
                timeout_seconds=args.timeout,
            )
        )
    except PlaywrightRefreshError as exc:
        print(f"Refresh failed: {exc}")
        raise SystemExit(1) from exc
    print(f"Token saved to {path}.")


def probe_models_command(args: argparse.Namespace) -> None:
    try:
        asyncio.run(
            run_probe(
                profile_dir=None,
                login_url=args.login_url,
                target_model=args.target_model,
                prompt="Reply with only OK.",
                headless=not args.headed,
                timeout_seconds=args.timeout,
            )
        )
    except PlaywrightRefreshError as exc:
        print(f"Model probe failed: {exc}")
        raise SystemExit(1) from exc
    except Exception as exc:
        print(f"Model probe failed: {exc}")
        raise SystemExit(1) from exc


def refresh_daemon_command(_args: argparse.Namespace) -> None:
    settings = Settings()
    try:
        asyncio.run(run_refresh_daemon(settings))
    except KeyboardInterrupt:
        print("Refresh daemon stopped.")


def launch_edge_command(_args: argparse.Namespace) -> None:
    if sys.platform != "win32":
        print("launch-edge is only available on Windows.")
        raise SystemExit(1)

    subprocess.Popen([
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "--remote-debugging-port=9222",
        "https://m365.cloud.microsoft/chat",
    ])
    print("Edge launched with remote debugging on port 9222.")


def serve_command(args: argparse.Namespace) -> None:
    uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
