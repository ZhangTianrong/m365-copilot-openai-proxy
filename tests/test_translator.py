from __future__ import annotations

from m365_copilot_openai_proxy.models import OpenAIChatRequest, OpenAIResponsesRequest
from m365_copilot_openai_proxy.proxy_profiles import resolve_proxy_profile
from m365_copilot_openai_proxy.translator import translate_openai_request, translate_responses_request
from m365_copilot_openai_proxy.conversation_reuse import (
    compute_advanced_history_hash,
    compute_prior_history_hash,
)


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

    assert translated.prompt == "Compare these\n\nAttached images for this message: [Image 1], [Image 2]"
    assert [image.filename for image in translated.images] == ["image.png", "image-2.jpg"]
    assert [image.filename for image in translated.current_images] == ["image.png", "image-2.jpg"]
    assert [image.content for image in translated.images] == [b"hello", b"world"]


def test_translate_openai_request_preserves_images_outside_final_user_message() -> None:
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
    assert translated.additional_context == [
        "Prior conversation transcript:\nUser: Earlier\n\nAttached images for this message: [Image 1]"
    ]
    assert [image.filename for image in translated.images] == ["image.png"]
    assert translated.current_images == []
    assert translated.images[0].content == b"hello"


def test_translate_openai_request_numbers_current_message_images_from_one() -> None:
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
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Now compare"},
                        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,d29ybGQ="}},
                    ],
                },
            ],
        }
    )

    translated = translate_openai_request(request)

    assert translated.additional_context == [
        "Prior conversation transcript:\nUser: Earlier\n\nAttached images for this message: [Image 1]"
    ]
    assert translated.prompt == "Now compare\n\nAttached images for this message: [Image 1]"
    assert [image.filename for image in translated.images] == ["image.png", "image-2.jpg"]
    assert [image.filename for image in translated.current_images] == ["image-2.jpg"]


def test_translate_responses_request_numbers_current_message_images_from_one() -> None:
    request = OpenAIResponsesRequest.model_validate(
        {
            "model": "ignored",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Earlier"},
                        {"type": "input_image", "image_url": "data:image/png;base64,aGVsbG8="},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Now compare"},
                        {"type": "input_image", "image_url": "data:image/jpeg;base64,d29ybGQ="},
                    ],
                },
            ],
        }
    )

    translated = translate_responses_request(request)

    assert translated.additional_context == [
        "Prior conversation transcript:\nUser: Earlier\n\nAttached images for this message: [Image 1]"
    ]
    assert translated.prompt == "Now compare\n\nAttached images for this message: [Image 1]"
    assert [image.filename for image in translated.images] == ["image.png", "image-2.jpg"]
    assert [image.filename for image in translated.current_images] == ["image-2.jpg"]


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


def test_translate_openai_request_ignores_data_url_files() -> None:
    request = OpenAIChatRequest.model_validate(
        {
            "model": "ignored",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Summarize this file"},
                        {
                            "type": "file",
                            "file": {
                                "filename": "receipt.pdf",
                                "file_data": "data:application/pdf;base64,aGVsbG8=",
                            },
                        },
                    ],
                }
            ],
        }
    )

    translated = translate_openai_request(request)

    assert translated.prompt == "Summarize this file"
    assert translated.attachments == []


def test_translate_responses_request_ignores_input_file_parts() -> None:
    request = OpenAIResponsesRequest.model_validate(
        {
            "model": "ignored",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Read this"},
                        {
                            "type": "input_file",
                            "filename": "notes.txt",
                            "file_data": "data:text/plain;base64,aGVsbG8=",
                        },
                    ],
                }
            ],
        }
    )

    translated = translate_responses_request(request)

    assert translated.prompt == "Read this"
    assert translated.attachments == []


def test_prior_history_hash_excludes_current_final_user_turn() -> None:
    request = OpenAIChatRequest.model_validate(
        {
            "model": "ignored",
            "messages": [
                {"role": "user", "content": "Earlier"},
                {"role": "assistant", "content": "Reply"},
                {"role": "user", "content": "First latest prompt"},
            ],
        }
    )
    altered_request = OpenAIChatRequest.model_validate(
        {
            "model": "ignored",
            "messages": [
                {"role": "user", "content": "Earlier"},
                {"role": "assistant", "content": "Reply"},
                {"role": "user", "content": "Different latest prompt"},
            ],
        }
    )

    translated = translate_openai_request(request)
    altered = translate_openai_request(altered_request)

    assert compute_prior_history_hash(translated) == compute_prior_history_hash(altered)


def test_advanced_history_hash_includes_current_user_turn_and_assistant_response() -> None:
    request = OpenAIChatRequest.model_validate(
        {
            "model": "ignored",
            "messages": [
                {"role": "user", "content": "Earlier"},
                {"role": "assistant", "content": "Reply"},
                {"role": "user", "content": "Latest prompt"},
            ],
        }
    )
    translated = translate_openai_request(request)

    first_hash = compute_advanced_history_hash(translated, "Assistant one")
    second_hash = compute_advanced_history_hash(translated, "Assistant two")

    assert first_hash != second_hash


def test_openai_and_responses_translate_to_same_canonical_history() -> None:
    chat_request = OpenAIChatRequest.model_validate(
        {
            "model": "ignored",
            "messages": [
                {"role": "system", "content": "Be concise."},
                {"role": "user", "content": "Earlier"},
                {"role": "assistant", "content": "Reply"},
                {"role": "user", "content": "Next"},
            ],
        }
    )
    responses_request = OpenAIResponsesRequest.model_validate(
        {
            "model": "ignored",
            "instructions": "Be concise.",
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "Earlier"}]},
                {"role": "assistant", "content": [{"type": "input_text", "text": "Reply"}]},
                {"role": "user", "content": [{"type": "input_text", "text": "Next"}]},
            ],
        }
    )

    translated_chat = translate_openai_request(chat_request)
    translated_responses = translate_responses_request(responses_request)

    assert translated_chat.system_text == translated_responses.system_text
    assert translated_chat.prior_turns == translated_responses.prior_turns
    assert translated_chat.prompt == translated_responses.prompt
    assert compute_prior_history_hash(translated_chat) == compute_prior_history_hash(translated_responses)


def test_preprocess_injects_transport_context_without_changing_history_hash() -> None:
    request = OpenAIChatRequest.model_validate(
        {
            "model": "m365-minis",
            "messages": [
                {"role": "system", "content": "Be concise."},
                {"role": "user", "content": "Earlier"},
                {"role": "assistant", "content": "Reply"},
                {"role": "user", "content": "Next"},
            ],
        }
    )

    translated = translate_openai_request(request)
    profile = resolve_proxy_profile("m365-minis", "m365-copilot")
    preprocessed = profile.preprocess_translated(translated)

    assert translated.transport_additional_context == []
    assert preprocessed.transport_additional_context
    assert compute_prior_history_hash(translated) == compute_prior_history_hash(preprocessed)


def test_chat_and_responses_tool_history_normalize_to_same_hash() -> None:
    chat_request = OpenAIChatRequest.model_validate(
        {
            "model": "m365-minis",
            "messages": [
                {"role": "user", "content": "Earlier"},
                {
                    "role": "assistant",
                    "content": "我来调用终端。",
                    "tool_calls": [
                        {
                            "id": "call_123",
                            "type": "function",
                            "function": {
                                "name": "shell_execute",
                                "arguments": '{"cmd":"pwd"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_123",
                    "name": "shell_execute",
                    "content": "/workspace",
                },
                {"role": "user", "content": "Continue"},
            ],
        }
    )
    responses_request = OpenAIResponsesRequest.model_validate(
        {
            "model": "m365-minis",
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Earlier"}],
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "我来调用终端。"}],
                },
                {
                    "type": "function_call",
                    "call_id": "call_123",
                    "name": "shell_execute",
                    "arguments": '{"cmd":"pwd"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_123",
                    "name": "shell_execute",
                    "output": "/workspace",
                },
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Continue"}],
                },
            ],
        }
    )

    translated_chat = translate_openai_request(chat_request)
    translated_responses = translate_responses_request(responses_request)

    assert translated_chat.prior_turns == translated_responses.prior_turns
    assert translated_chat.prompt == translated_responses.prompt
    assert compute_prior_history_hash(translated_chat) == compute_prior_history_hash(translated_responses)


def test_translate_responses_request_allows_final_function_call_output() -> None:
    request = OpenAIResponsesRequest.model_validate(
        {
            "model": "m365-minis",
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Earlier"}],
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "我来调用终端。"}],
                },
                {
                    "type": "function_call",
                    "call_id": "call_123",
                    "name": "shell_execute",
                    "arguments": '{"cmd":"pwd"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_123",
                    "name": "shell_execute",
                    "output": "/workspace",
                },
            ],
        }
    )

    translated = translate_responses_request(request)

    assert translated.prompt.startswith("<proxy_tool_output>")
    assert translated.prompt_role == "tool"
    assert len(translated.prior_turns) == 2
    assert translated.prior_turns[0].role == "user"
    assert translated.prior_turns[1].role == "assistant"


def test_translate_openai_request_allows_final_tool_message() -> None:
    request = OpenAIChatRequest.model_validate(
        {
            "model": "m365-minis",
            "messages": [
                {"role": "user", "content": "Earlier"},
                {
                    "role": "assistant",
                    "content": "我来调用终端。",
                    "tool_calls": [
                        {
                            "id": "call_123",
                            "type": "function",
                            "function": {
                                "name": "shell_execute",
                                "arguments": '{"cmd":"pwd"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_123",
                    "name": "shell_execute",
                    "content": "/workspace",
                },
            ],
        }
    )

    translated = translate_openai_request(request)

    assert translated.prompt.startswith("<proxy_tool_output>")
    assert translated.prompt_role == "tool"
    assert len(translated.prior_turns) == 2
    assert translated.prior_turns[0].role == "user"
    assert translated.prior_turns[1].role == "assistant"
