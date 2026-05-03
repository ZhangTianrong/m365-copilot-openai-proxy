from __future__ import annotations

from m365_copilot_openai_proxy.models import OpenAIChatRequest, OpenAIResponsesRequest
from m365_copilot_openai_proxy.translator import translate_openai_request, translate_responses_request


def test_translate_openai_request_keeps_multiple_images_in_order() -> None:
    request = OpenAIChatRequest.model_validate(
        {
            "model": "ignored",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Compare these"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
                        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,d29ybGQ="}},
                    ],
                }
            ],
        }
    )

    translated = translate_openai_request(request)

    assert translated.prompt == "Compare these"
    assert [image.filename for image in translated.images] == ["image.png", "image-2.jpg"]
    assert [image.content for image in translated.images] == [b"hello", b"world"]


def test_translate_openai_request_drops_images_outside_final_user_message() -> None:
    request = OpenAIChatRequest.model_validate(
        {
            "model": "ignored",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Earlier"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
                    ],
                },
                {"role": "user", "content": "Final prompt"},
            ],
        }
    )

    translated = translate_openai_request(request)

    assert translated.prompt == "Final prompt"
    assert translated.additional_context == ["Prior conversation transcript:\nUser: Earlier"]
    assert translated.images == []


def test_translate_responses_request_rejects_non_image_data_urls() -> None:
    request = OpenAIResponsesRequest.model_validate(
        {
            "model": "ignored",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "image_url": "data:text/plain;base64,aGVsbG8="},
                    ],
                }
            ],
        }
    )

    translated = translate_responses_request(request)

    assert translated.prompt == ""
    assert translated.images == []
