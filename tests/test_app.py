from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi.testclient import TestClient

from m365_copilot_openai_proxy.app import create_app
from m365_copilot_openai_proxy.config import Settings
from m365_copilot_openai_proxy.graph_client import filter_reserved_scopes
from m365_copilot_openai_proxy.models import TranslatedImage


class FakeCopilotClient:
    def __init__(self):
        self.calls: list[tuple[str, list[str], list[TranslatedImage]]] = []

    async def chat(
        self,
        prompt: str,
        additional_context: list[str],
        images: list[TranslatedImage] | None = None,
    ) -> str:
        self.calls.append((prompt, additional_context, images or []))
        return "copilot reply"

    async def chat_stream(
        self,
        prompt: str,
        additional_context: list[str],
        images: list[TranslatedImage] | None = None,
    ) -> AsyncIterator[str]:
        self.calls.append((prompt, additional_context, images or []))
        yield "hello"
        yield " world"


def build_client(fake: FakeCopilotClient) -> TestClient:
    settings = Settings(_env_file=None, M365_ACCESS_TOKEN="fake-token")
    app = create_app(settings=settings, copilot_client_factory=lambda: fake)
    return TestClient(app)


def test_models_endpoint() -> None:
    client = build_client(FakeCopilotClient())
    response = client.get("/v1/models")
    assert response.status_code == 200
    body = response.json()
    assert body["data"][0]["id"] == "m365-copilot"


def test_openai_chat_completion_translates_history() -> None:
    fake = FakeCopilotClient()
    client = build_client(fake)
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
        (
            "Second question",
            [
                "System instructions:\nBe concise.",
                "Prior conversation transcript:\nUser: First question\nAssistant: First answer",
            ],
            [],
        )
    ]


def test_openai_chat_completion_supports_data_url_images() -> None:
    fake = FakeCopilotClient()
    client = build_client(fake)
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
    prompt, additional_context, images = fake.calls[0]
    assert prompt == "What is in this image?"
    assert additional_context == []
    assert len(images) == 1
    assert images[0].mime_type == "image/png"
    assert images[0].file_extension == "png"
    assert images[0].content == b"hello"


def test_openai_streaming_returns_sse() -> None:
    fake = FakeCopilotClient()
    client = build_client(fake)
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


def test_openai_chat_completion_drops_remote_image_urls() -> None:
    fake = FakeCopilotClient()
    client = build_client(fake)
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
    prompt, additional_context, images = fake.calls[0]
    assert prompt == "Describe this"
    assert additional_context == []
    assert images == []


def test_openai_responses_support_data_url_images() -> None:
    fake = FakeCopilotClient()
    client = build_client(fake)
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
    prompt, additional_context, images = fake.calls[0]
    assert prompt == "Identify this image"
    assert additional_context == []
    assert len(images) == 1
    assert images[0].mime_type == "image/webp"
    assert images[0].file_extension == "webp"


def test_anthropic_messages_drop_images() -> None:
    fake = FakeCopilotClient()
    client = build_client(fake)
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
    assert fake.calls == [("", [], [])]


def test_openai_chat_completion_drops_images_outside_final_user_message() -> None:
    fake = FakeCopilotClient()
    client = build_client(fake)
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
        (
            "Second question",
            ["Prior conversation transcript:\nUser: First question"],
            [],
        )
    ]


def test_anthropic_messages_endpoint() -> None:
    fake = FakeCopilotClient()
    client = build_client(fake)
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


def test_reserved_scopes_are_filtered_from_msal_requests() -> None:
    assert filter_reserved_scopes(
        [
            "Mail.Read",
            "offline_access",
            "openid",
            "Chat.Read",
        ]
    ) == ["Mail.Read", "Chat.Read"]
