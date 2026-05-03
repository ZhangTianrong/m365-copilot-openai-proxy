from __future__ import annotations

import argparse
import asyncio
import re
import subprocess
import sys

import uvicorn

from .app import create_app
from .config import Settings
from .playwright_refresh import (
    PlaywrightRefreshError,
    export_cookies_with_playwright,
    import_cookies_with_playwright,
    login_with_playwright,
    refresh_token_with_playwright,
    run_refresh_daemon,
)
from .token_store import write_access_token


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
    login_parser.add_argument("--cookies", dest="cookies_path")
    login_parser.add_argument("--headless", action="store_true")
    login_parser.add_argument("--timeout", type=int, default=600)
    login_parser.set_defaults(func=login_command)

    import_cookies_parser = subparsers.add_parser("import-cookies")
    import_cookies_parser.add_argument("cookies_path")
    import_cookies_parser.add_argument("--headed", action="store_true")
    import_cookies_parser.set_defaults(func=import_cookies_command)

    export_cookies_parser = subparsers.add_parser("export-cookies")
    export_cookies_parser.add_argument("output_path")
    export_cookies_parser.add_argument("--headed", action="store_true")
    export_cookies_parser.set_defaults(func=export_cookies_command)

    refresh_parser = subparsers.add_parser("refresh-token")
    refresh_parser.add_argument("--headed", action="store_true")
    refresh_parser.add_argument("--timeout", type=int, default=90)
    refresh_parser.set_defaults(func=refresh_token_command)

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
    print("Paste the full WebSocket URL (or just the access_token value), then press Enter:")
    raw = input().strip()
    token = _extract_token(raw)
    if not token:
        print("Error: could not find a valid token. Make sure you copied the full WebSocket URL.")
        raise SystemExit(1)
    path = write_access_token(settings, token)
    print(f"Token saved to {path}.")


def login_command(args: argparse.Namespace) -> None:
    settings = Settings()
    if args.cookies_path:
        mode = "headless" if args.headless else "headed"
        print(
            f"Launching a persistent Playwright browser profile in {mode} mode, importing cookies "
            f"from {args.cookies_path}, and waiting for the Copilot token."
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
                cookies_path=args.cookies_path,
            )
        )
    except PlaywrightRefreshError as exc:
        print(f"Login failed: {exc}")
        raise SystemExit(1) from exc
    print(f"Token saved to {path}.")


def import_cookies_command(args: argparse.Namespace) -> None:
    settings = Settings()
    try:
        count = asyncio.run(
            import_cookies_with_playwright(
                settings,
                args.cookies_path,
                headed=args.headed,
            )
        )
    except PlaywrightRefreshError as exc:
        print(f"Cookie import failed: {exc}")
        raise SystemExit(1) from exc
    print(f"Imported {count} cookies into {settings.profile_dir}.")


def export_cookies_command(args: argparse.Namespace) -> None:
    settings = Settings()
    try:
        path, count = asyncio.run(
            export_cookies_with_playwright(
                settings,
                args.output_path,
                headed=args.headed,
            )
        )
    except PlaywrightRefreshError as exc:
        print(f"Cookie export failed: {exc}")
        raise SystemExit(1) from exc
    print(f"Exported {count} cookies from {settings.profile_dir} to {path}.")


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
