from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ImageURLPart(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str


class ContentPart(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str
    text: str | None = None
    image_url: str | ImageURLPart | None = None


class OpenAIMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | list[ContentPart]


class OpenAIChatRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str
    messages: list[OpenAIMessage]
    stream: bool = False
    temperature: float | None = None
    user: str | None = None


class AnthropicMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: Literal["user", "assistant"]
    content: str | list[ContentPart]


class AnthropicMessagesRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str
    messages: list[AnthropicMessage]
    system: str | list[ContentPart] | None = None
    stream: bool = False
    max_tokens: int | None = None
    temperature: float | None = None


class CopilotMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    text: str = ""
    attributions: list[dict[str, Any]] = Field(default_factory=list)


class CopilotConversation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    messages: list[CopilotMessage] = Field(default_factory=list)


class OpenAIResponsesRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str
    input: str | list[Any]
    instructions: str | None = None
    stream: bool = False


class TranslatedAttachment(BaseModel):
    kind: Literal["image", "file"] = "image"
    filename: str
    mime_type: str
    file_extension: str
    data_url: str
    content: bytes


class UploadedAttachment(BaseModel):
    kind: Literal["image", "file"] = "image"
    doc_id: str
    file_name: str
    file_type: str
    uploaded_file_name: str | None = None
    logical_id: str | None = None
    entity_id: str | None = None
    annotation_type: str | None = None
    annotation_text: str | None = None
    annotation_url: str | None = None


class HistoryTurn(BaseModel):
    role: str
    text: str


class TranslatedRequest(BaseModel):
    prompt: str
    additional_context: list[str] = Field(default_factory=list)
    attachments: list[TranslatedAttachment] = Field(default_factory=list)
    current_attachments: list[TranslatedAttachment] = Field(default_factory=list)
    system_text: str = ""
    prior_turns: list[HistoryTurn] = Field(default_factory=list)

    @property
    def images(self) -> list[TranslatedAttachment]:
        return self.attachments

    @property
    def current_images(self) -> list[TranslatedAttachment]:
        return self.current_attachments


AccountMode = Literal["enterprise", "personal"]


class AuthSessionSnapshot(BaseModel):
    account_mode: AccountMode
    access_token: str
    expires_at: int | None = None
    captured_at: int
    oid: str
    tid: str
    websocket_url: str | None = None
    graph_access_token: str | None = None
    graph_expires_at: int | None = None
    search_access_token: str | None = None
    search_expires_at: int | None = None


TranslatedImage = TranslatedAttachment
UploadedImage = UploadedAttachment
