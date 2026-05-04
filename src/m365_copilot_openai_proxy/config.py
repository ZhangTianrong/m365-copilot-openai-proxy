from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    access_token: str | None = Field(default=None, alias="M365_ACCESS_TOKEN")
    access_token_file: str = Field(default=".state/access_token.txt", alias="M365_ACCESS_TOKEN_FILE")
    profile_dir: str = Field(default=".state/profile", alias="M365_PROFILE_DIR")
    debug_logging: bool = Field(default=False, alias="M365_DEBUG_LOGGING")
    enable_conversation_reuse: bool = Field(default=False, alias="M365_ENABLE_CONVERSATION_REUSE")
    conversation_db_path: str = Field(
        default=".state/conversation_reuse.db",
        alias="M365_CONVERSATION_DB_PATH",
    )
    conversation_max_conversations: int = Field(
        default=500,
        alias="M365_CONVERSATION_MAX_CONVERSATIONS",
    )
    login_url: str = Field(default="https://m365.cloud.microsoft/chat", alias="M365_LOGIN_URL")
    browser_channel: str | None = Field(default=None, alias="M365_BROWSER_CHANNEL")
    login_email: str | None = Field(default=None, alias="M365_LOGIN_EMAIL")
    login_password: str | None = Field(default=None, alias="M365_LOGIN_PASSWORD")
    login_totp_secret: str | None = Field(default=None, alias="M365_LOGIN_TOTP_SECRET")
    token_capture_timeout_seconds: int = Field(default=600, alias="M365_TOKEN_CAPTURE_TIMEOUT_SECONDS")
    token_refresh_buffer_seconds: int = Field(default=300, alias="M365_TOKEN_REFRESH_BUFFER_SECONDS")
    token_refresh_retry_seconds: int = Field(default=30, alias="M365_TOKEN_REFRESH_RETRY_SECONDS")
    time_zone: str = Field(default="Asia/Tokyo", alias="M365_TIME_ZONE")
    model_alias: str = Field(default="m365-copilot", alias="M365_MODEL_ALIAS")
