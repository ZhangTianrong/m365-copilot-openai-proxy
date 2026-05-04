from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi.testclient import TestClient

from m365_copilot_openai_proxy.app import create_app
from m365_copilot_openai_proxy.config import Settings
from m365_copilot_openai_proxy.graph_client import filter_reserved_scopes
from m365_copilot_openai_proxy.models import TranslatedImage


class FakeCopilotClient:
    def __init__(
        self,
        *,
        text_response: str = "copilot reply",
        stream_chunks: list[str] | None = None,
    ):
        self.calls: list[dict[str, object]] = []
        self.text_response = text_response
        self.stream_chunks = stream_chunks or ["hello", " world"]

    async def chat(
        self,
        prompt: str,
        additional_context: list[str],
        images: list[TranslatedImage] | None = None,
        *,
        conversation_id: str,
        is_start_of_session: bool,
    ) -> str:
        self.calls.append(
            {
                "prompt": prompt,
                "additional_context": additional_context,
                "images": images or [],
                "conversation_id": conversation_id,
                "is_start_of_session": is_start_of_session,
            }
        )
        return self.text_response

    async def chat_stream(
        self,
        prompt: str,
        additional_context: list[str],
        images: list[TranslatedImage] | None = None,
        *,
        conversation_id: str,
        is_start_of_session: bool,
    ) -> AsyncIterator[str]:
        self.calls.append(
            {
                "prompt": prompt,
                "additional_context": additional_context,
                "images": images or [],
                "conversation_id": conversation_id,
                "is_start_of_session": is_start_of_session,
            }
        )
        for chunk in self.stream_chunks:
            yield chunk


def build_client(
    fake: FakeCopilotClient,
    tmp_path,
    *,
    enable_reuse: bool = False,
) -> TestClient:
    settings = Settings(
        _env_file=None,
        M365_ACCESS_TOKEN="fake-token",
        M365_ENABLE_CONVERSATION_REUSE=enable_reuse,
        M365_CONVERSATION_DB_PATH=str(tmp_path / "conversation_reuse.db"),
    )
    app = create_app(settings=settings, copilot_client_factory=lambda: fake)
    return TestClient(app)


def test_models_endpoint(tmp_path) -> None:
    client = build_client(FakeCopilotClient(), tmp_path)
    response = client.get("/v1/models")
    assert response.status_code == 200
    body = response.json()
    assert body["data"][0]["id"] == "m365-copilot"


def test_openai_chat_completion_translates_history(tmp_path) -> None:
    fake = FakeCopilotClient()
    client = build_client(fake, tmp_path)
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "ignored",
            "messages": [
                {"role": "system", "content": "Be concise."},
                {"role": "user", "content": "First question"},
                {"role": "assistant", "content": "First answer"},
                {"role": "user", "content": "Second question"},
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "copilot reply"
    assert fake.calls == [
        {
            "prompt": "Second question",
            "additional_context": [
                "System instructions:\nBe concise.",
                "Prior conversation transcript:\nUser: First question\nAssistant: First answer",
            ],
            "images": [],
            "conversation_id": fake.calls[0]["conversation_id"],
            "is_start_of_session": True,
        }
    ]


def test_openai_chat_completion_supports_data_url_images(tmp_path) -> None:
    fake = FakeCopilotClient()
    client = build_client(fake, tmp_path)
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "ignored",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What is in this image?"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
                    ],
                }
            ],
        },
    )
    assert response.status_code == 200
    call = fake.calls[0]
    assert call["prompt"] == "What is in this image?\n\nAttached images for this message: [Image 1]"
    assert call["additional_context"] == []
    images = call["images"]
    assert len(images) == 1
    assert images[0].mime_type == "image/png"
    assert images[0].file_extension == "png"
    assert images[0].content == b"hello"


def test_openai_streaming_returns_sse(tmp_path) -> None:
    fake = FakeCopilotClient()
    client = build_client(fake, tmp_path)
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "ignored",
            "stream": True,
            "messages": [{"role": "user", "content": "Hello"}],
        },
    ) as response:
        payload = "".join(
            chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
            for chunk in response.iter_text()
        )
    assert response.status_code == 200
    assert '"role": "assistant"' in payload
    assert '"content": "hello"' in payload
    assert '"content": " world"' in payload
    assert "data: [DONE]" in payload


def test_openai_chat_completion_drops_remote_image_urls(tmp_path) -> None:
    fake = FakeCopilotClient()
    client = build_client(fake, tmp_path)
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "ignored",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this"},
                        {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}},
                    ],
                }
            ],
        },
    )
    assert response.status_code == 200
    call = fake.calls[0]
    assert call["prompt"] == "Describe this"
    assert call["additional_context"] == []
    assert call["images"] == []


def test_openai_responses_support_data_url_images(tmp_path) -> None:
    fake = FakeCopilotClient()
    client = build_client(fake, tmp_path)
    response = client.post(
        "/v1/responses",
        json={
            "model": "ignored",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Identify this image"},
                        {"type": "input_image", "image_url": "data:image/webp;base64,aGVsbG8="},
                    ],
                }
            ],
        },
    )
    assert response.status_code == 200
    call = fake.calls[0]
    assert call["prompt"] == "Identify this image\n\nAttached images for this message: [Image 1]"
    assert call["additional_context"] == []
    images = call["images"]
    assert len(images) == 1
    assert images[0].mime_type == "image/webp"
    assert images[0].file_extension == "webp"


def test_anthropic_messages_drop_images(tmp_path) -> None:
    fake = FakeCopilotClient()
    client = build_client(fake, tmp_path)
    response = client.post(
        "/v1/messages",
        json={
            "model": "ignored",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
                    ],
                }
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "message"
    assert body["content"][0]["text"] == "copilot reply"
    assert fake.calls == [
        {
            "prompt": "",
            "additional_context": [],
            "images": [],
            "conversation_id": fake.calls[0]["conversation_id"],
            "is_start_of_session": True,
        }
    ]


def test_openai_chat_completion_preserves_images_outside_final_user_message(tmp_path) -> None:
    fake = FakeCopilotClient()
    client = build_client(fake, tmp_path)
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "ignored",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "First question"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
                    ],
                },
                {"role": "user", "content": "Second question"},
            ],
        },
    )
    assert response.status_code == 200
    assert fake.calls == [
        {
            "prompt": "Second question",
            "additional_context": [
                "Prior conversation transcript:\nUser: First question\n\nAttached images for this message: [Image 1]"
            ],
            "images": [
                TranslatedImage(
                    filename="image.png",
                    mime_type="image/png",
                    file_extension="png",
                    data_url="data:image/png;base64,aGVsbG8=",
                    content=b"hello",
                )
            ],
            "conversation_id": fake.calls[0]["conversation_id"],
            "is_start_of_session": True,
        }
    ]


def test_openai_chat_completion_numbers_history_and_final_images_together(tmp_path) -> None:
    fake = FakeCopilotClient()
    client = build_client(fake, tmp_path)
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "ignored",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Earlier"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Now compare"},
                        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,d29ybGQ="}},
                    ],
                },
            ],
        },
    )
    assert response.status_code == 200
    call = fake.calls[0]
    assert call["prompt"] == "Now compare\n\nAttached images for this message: [Image 2]"
    assert call["additional_context"] == [
        "Prior conversation transcript:\nUser: Earlier\n\nAttached images for this message: [Image 1]"
    ]
    assert [image.filename for image in call["images"]] == ["image.png", "image-2.jpg"]


def test_anthropic_messages_endpoint(tmp_path) -> None:
    fake = FakeCopilotClient()
    client = build_client(fake, tmp_path)
    response = client.post(
        "/v1/messages",
        json={
            "model": "ignored",
            "system": "Be concise.",
            "messages": [{"role": "user", "content": "Hello"}],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "message"
    assert body["content"][0]["text"] == "copilot reply"


def test_reuse_disabled_preserves_stateless_behavior(tmp_path) -> None:
    fake = FakeCopilotClient()
    client = build_client(fake, tmp_path, enable_reuse=False)
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "ignored",
            "messages": [
                {"role": "user", "content": "Earlier"},
                {"role": "assistant", "content": "Reply"},
                {"role": "user", "content": "Next"},
            ],
            "user": "alice",
        },
    )
    assert response.status_code == 200
    call = fake.calls[0]
    assert call["is_start_of_session"] is True
    assert call["additional_context"] == [
        "Prior conversation transcript:\nUser: Earlier\nAssistant: Reply"
    ]


def test_reuse_enabled_reuses_latest_history_key_for_same_user(tmp_path) -> None:
    fake = FakeCopilotClient()
    client = build_client(fake, tmp_path, enable_reuse=True)

    first = client.post(
        "/v1/chat/completions",
        json={
            "model": "ignored",
            "messages": [
                {"role": "user", "content": "Earlier"},
                {"role": "assistant", "content": "Reply"},
                {"role": "user", "content": "Next"},
            ],
            "user": "alice",
        },
    )
    assert first.status_code == 200
    first_call = fake.calls[0]

    second = client.post(
        "/v1/chat/completions",
        json={
            "model": "ignored",
            "messages": [
                {"role": "user", "content": "Earlier"},
                {"role": "assistant", "content": "Reply"},
                {"role": "user", "content": "Next"},
                {"role": "assistant", "content": "copilot reply"},
                {"role": "user", "content": "Follow up"},
            ],
            "user": "alice",
        },
    )
    assert second.status_code == 200
    second_call = fake.calls[1]
    assert second_call["conversation_id"] == first_call["conversation_id"]
    assert second_call["is_start_of_session"] is False
    assert second_call["prompt"] == "Follow up"
    assert second_call["additional_context"] == []


def test_reuse_enabled_branches_via_stateless_fallback_when_latest_key_no_longer_matches(tmp_path) -> None:
    fake = FakeCopilotClient()
    client = build_client(fake, tmp_path, enable_reuse=True)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "ignored",
            "messages": [
                {"role": "user", "content": "Earlier"},
                {"role": "assistant", "content": "Reply"},
                {"role": "user", "content": "Next"},
            ],
            "user": "alice",
        },
    )
    assert response.status_code == 200
    first_call = fake.calls[0]

    branch = client.post(
        "/v1/chat/completions",
        json={
            "model": "ignored",
            "messages": [
                {"role": "user", "content": "Earlier"},
                {"role": "assistant", "content": "Reply"},
                {"role": "user", "content": "Alternative branch"},
            ],
            "user": "alice",
        },
    )
    assert branch.status_code == 200
    branch_call = fake.calls[1]
    assert branch_call["conversation_id"] != first_call["conversation_id"]
    assert branch_call["is_start_of_session"] is True
    assert branch_call["additional_context"] == [
        "Prior conversation transcript:\nUser: Earlier\nAssistant: Reply"
    ]


def test_reuse_scope_uses_openai_user_field(tmp_path) -> None:
    fake = FakeCopilotClient()
    client = build_client(fake, tmp_path, enable_reuse=True)

    first = client.post(
        "/v1/chat/completions",
        json={
            "model": "ignored",
            "messages": [
                {"role": "user", "content": "Earlier"},
                {"role": "assistant", "content": "Reply"},
                {"role": "user", "content": "Next"},
            ],
            "user": "alice",
        },
    )
    assert first.status_code == 200
    first_call = fake.calls[0]

    second = client.post(
        "/v1/chat/completions",
        json={
            "model": "ignored",
            "messages": [
                {"role": "user", "content": "Earlier"},
                {"role": "assistant", "content": "Reply"},
                {"role": "user", "content": "Next"},
                {"role": "assistant", "content": "copilot reply"},
                {"role": "user", "content": "Follow up"},
            ],
            "user": "bob",
        },
    )
    assert second.status_code == 200
    second_call = fake.calls[1]
    assert second_call["conversation_id"] != first_call["conversation_id"]
    assert second_call["is_start_of_session"] is True


def test_reserved_scopes_are_filtered_from_msal_requests() -> None:
    assert filter_reserved_scopes(
        [
            "Mail.Read",
            "offline_access",
            "openid",
            "Chat.Read",
        ]
    ) == ["Mail.Read", "Chat.Read"]
