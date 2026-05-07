from __future__ import annotations

from m365_copilot_openai_proxy.config import Settings
from m365_copilot_openai_proxy.token_store import (
    TokenStoreError,
    auth_session_expires_at,
    auth_session_needs_refresh,
    build_enterprise_auth_session,
    load_access_token,
    load_auth_session,
    parse_websocket_url,
    write_access_token,
    write_auth_session,
)

_TEST_JWT = (
    "eyJhbGciOiJub25lIn0."
    "eyJvaWQiOiIxMjM0NTY3OC0xMjM0LTEyMzQtMTIzNC0xMjM0NTY3ODkwYWIiLCJ0aWQiOiJhYmNkZWYwMS0yMzQ1LTY3ODktYWJjZC1lZjAxMjM0NTY3ODkiLCJleHAiOjQxMDAwMDAwMDB9."
)

_PERSONAL_WS_URL = (
    "wss://substrate.office.com/m365Copilot/Chathub/"
    "00000000-0000-0000-853e-527a6bf3c11e@84df9e7f-e9f6-40af-b435-aaaaaaaaaaaa"
    "?access_token=eyJhbGciOiJkaXIifQ.test.encrypted.value.more"
)


def test_build_enterprise_auth_session_derives_expiry_and_identity() -> None:
    auth_session = build_enterprise_auth_session(_TEST_JWT)

    assert auth_session.account_mode == "enterprise"
    assert auth_session.access_token == _TEST_JWT
    assert auth_session.expires_at == 4100000000
    assert auth_session.oid == "12345678-1234-1234-1234-1234567890ab"
    assert auth_session.tid == "abcdef01-2345-6789-abcd-ef0123456789"


def test_parse_websocket_url_builds_personal_auth_session() -> None:
    auth_session = parse_websocket_url(_PERSONAL_WS_URL)

    assert auth_session.account_mode == "personal"
    assert auth_session.access_token == "eyJhbGciOiJkaXIifQ.test.encrypted.value.more"
    assert auth_session.oid == "00000000-0000-0000-853e-527a6bf3c11e"
    assert auth_session.tid == "84df9e7f-e9f6-40af-b435-aaaaaaaaaaaa"
    assert auth_session.websocket_url == _PERSONAL_WS_URL


def test_load_auth_session_prefers_auth_state_file(tmp_path) -> None:
    state_path = tmp_path / "auth_session.json"
    token_path = tmp_path / "access_token.txt"
    settings = Settings(
        _env_file=None,
        M365_ACCOUNT_MODE="enterprise",
        M365_AUTH_STATE_FILE=str(state_path),
        M365_ACCESS_TOKEN_FILE=str(token_path),
    )
    write_auth_session(settings, build_enterprise_auth_session(_TEST_JWT))

    auth_session = load_auth_session(settings)

    assert auth_session.account_mode == "enterprise"
    assert token_path.read_text(encoding="utf-8").strip() == _TEST_JWT


def test_load_auth_session_falls_back_to_enterprise_token_sources(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        M365_ACCOUNT_MODE="enterprise",
        M365_ACCESS_TOKEN=_TEST_JWT,
        M365_AUTH_STATE_FILE=str(tmp_path / "missing-auth.json"),
        M365_ACCESS_TOKEN_FILE=str(tmp_path / "missing-token.txt"),
    )

    auth_session = load_auth_session(settings)

    assert auth_session.account_mode == "enterprise"
    assert auth_session.access_token == _TEST_JWT


def test_load_auth_session_rejects_personal_mode_without_snapshot(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        M365_ACCOUNT_MODE="personal",
        M365_AUTH_STATE_FILE=str(tmp_path / "missing-auth.json"),
        M365_ACCESS_TOKEN_FILE=str(tmp_path / "missing-token.txt"),
    )

    try:
        load_auth_session(settings)
    except TokenStoreError as exc:
        assert "No personal auth session is configured" in str(exc)
    else:
        raise AssertionError("Expected a TokenStoreError when no personal auth snapshot exists.")


def test_load_auth_session_rejects_saved_mode_mismatch(tmp_path) -> None:
    state_path = tmp_path / "auth_session.json"
    settings = Settings(
        _env_file=None,
        M365_ACCOUNT_MODE="enterprise",
        M365_AUTH_STATE_FILE=str(state_path),
        M365_ACCESS_TOKEN_FILE=str(tmp_path / "access_token.txt"),
    )
    personal_session = parse_websocket_url(_PERSONAL_WS_URL)
    personal_session.expires_at = 4100000000
    write_auth_session(
        Settings(
            _env_file=None,
            M365_ACCOUNT_MODE="personal",
            M365_AUTH_STATE_FILE=str(state_path),
            M365_ACCESS_TOKEN_FILE=str(tmp_path / "access_token.txt"),
        ),
        personal_session,
    )

    try:
        load_auth_session(settings)
    except TokenStoreError as exc:
        assert "account mode mismatch" in str(exc)
    else:
        raise AssertionError("Expected a TokenStoreError when auth snapshot mode mismatches.")


def test_write_access_token_creates_enterprise_auth_state_and_token_mirror(tmp_path) -> None:
    state_path = tmp_path / "nested" / "auth_session.json"
    token_path = tmp_path / "nested" / "access_token.txt"
    settings = Settings(
        _env_file=None,
        M365_ACCOUNT_MODE="enterprise",
        M365_AUTH_STATE_FILE=str(state_path),
        M365_ACCESS_TOKEN_FILE=str(token_path),
    )

    written_path = write_access_token(settings, _TEST_JWT)

    assert written_path == token_path
    assert token_path.read_text(encoding="utf-8") == _TEST_JWT + "\n"
    assert load_auth_session(settings).account_mode == "enterprise"


def test_load_access_token_reads_from_auth_session(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        M365_ACCOUNT_MODE="enterprise",
        M365_AUTH_STATE_FILE=str(tmp_path / "auth_session.json"),
        M365_ACCESS_TOKEN_FILE=str(tmp_path / "access_token.txt"),
    )
    write_auth_session(settings, build_enterprise_auth_session(_TEST_JWT))

    assert load_access_token(settings) == _TEST_JWT


def test_auth_session_needs_refresh_uses_expires_at() -> None:
    auth_session = build_enterprise_auth_session(_TEST_JWT)

    assert auth_session_needs_refresh(auth_session, 9999999999) is True


def test_auth_session_expires_at_uses_earliest_graph_search_or_copilot_expiry() -> None:
    auth_session = build_enterprise_auth_session(_TEST_JWT)
    auth_session.graph_expires_at = 4000000000
    auth_session.search_expires_at = 3900000000

    assert auth_session_expires_at(auth_session) == 3900000000


def test_auth_session_expires_at_personal_prefers_graph_and_search_expiry() -> None:
    auth_session = parse_websocket_url(_PERSONAL_WS_URL)
    auth_session.expires_at = 3800000000
    auth_session.graph_expires_at = 4100000000
    auth_session.search_expires_at = 4000000000

    assert auth_session_expires_at(auth_session) == 4000000000


def test_auth_session_expires_at_personal_falls_back_to_primary_expiry_without_upload_tokens() -> None:
    auth_session = parse_websocket_url(_PERSONAL_WS_URL)
    auth_session.expires_at = 3800000000

    assert auth_session_expires_at(auth_session) == 3800000000
