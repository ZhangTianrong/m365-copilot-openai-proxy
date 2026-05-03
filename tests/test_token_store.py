from __future__ import annotations

from m365_copilot_openai_proxy.config import Settings
from m365_copilot_openai_proxy.token_store import (
    TokenStoreError,
    load_access_token,
    write_access_token,
)


def test_load_access_token_prefers_file(tmp_path) -> None:
    token_path = tmp_path / "access_token.txt"
    token_path.write_text("file-token\n", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        M365_ACCESS_TOKEN="env-token",
        M365_ACCESS_TOKEN_FILE=str(token_path),
    )

    assert load_access_token(settings) == "file-token"


def test_load_access_token_falls_back_to_env() -> None:
    settings = Settings(
        _env_file=None,
        M365_ACCESS_TOKEN="env-token",
        M365_ACCESS_TOKEN_FILE="missing-token.txt",
    )

    assert load_access_token(settings) == "env-token"


def test_load_access_token_requires_configured_source(tmp_path) -> None:
    settings = Settings(_env_file=None, M365_ACCESS_TOKEN_FILE=str(tmp_path / "missing-token.txt"))

    try:
        load_access_token(settings)
    except TokenStoreError as exc:
        assert "No access token is configured" in str(exc)
    else:
        raise AssertionError("Expected a TokenStoreError when no token source exists.")


def test_write_access_token_creates_parent_directory(tmp_path) -> None:
    token_path = tmp_path / "nested" / "access_token.txt"
    settings = Settings(_env_file=None, M365_ACCESS_TOKEN_FILE=str(token_path))

    written_path = write_access_token(settings, "new-token")

    assert written_path == token_path
    assert token_path.read_text(encoding="utf-8") == "new-token\n"
