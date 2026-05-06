from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Iterable

from .models import (
    AnthropicMessagesRequest,
    ContentPart,
    HistoryTurn,
    OpenAIChatRequest,
    OpenAIToolCall,
    OpenAIResponsesRequest,
    TranslatedImage,
    TranslatedRequest,
)
from .proxy_profiles import canonicalize_assistant_turn, canonicalize_tool_output

_SUPPORTED_IMAGE_MIME_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
}
_DATA_URL_RE = re.compile(r"^data:(?P<mime>[-\w.+/]+);base64,(?P<data>.+)$", re.DOTALL)


def flatten_content(content: str | list[ContentPart] | None, *, context: str) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    text_parts: list[str] = []
    for part in content:
        if part.type in {"text", "input_text", "output_text"}:
            text_parts.append(part.text or "")
    return "".join(text_parts)


def _join_lines(lines: Iterable[str]) -> str:
    return "\n".join(line for line in lines if line).strip()


def _parse_image_data_url(data_url: str, *, image_index: int) -> TranslatedImage:
    match = _DATA_URL_RE.match(data_url)
    if not match:
        if data_url.startswith(("http://", "https://")):
            raise ValueError("Remote image URLs are not supported. Use a data URL instead.")
        raise ValueError("Image attachments must use a base64 data URL.")

    mime_type = match.group("mime").lower()
    file_extension = _SUPPORTED_IMAGE_MIME_TYPES.get(mime_type)
    if not file_extension:
        raise ValueError(
            "Unsupported image MIME type. Only PNG, JPEG, and WebP data URLs are supported."
        )

    encoded = match.group("data")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Image attachment data URL is not valid base64.") from exc

    filename = f"image.{file_extension}" if image_index == 1 else f"image-{image_index}.{file_extension}"
    return TranslatedImage(
        filename=filename,
        mime_type=mime_type,
        file_extension=file_extension,
        data_url=data_url,
        content=content,
    )


def _extract_openai_content(
    content: str | list[ContentPart] | None,
    *,
    allow_images: bool,
    attachment_offset: int,
) -> tuple[str, list[TranslatedImage], list[str]]:
    if content is None:
        return "", [], []
    if isinstance(content, str):
        return content, [], []

    text_parts: list[str] = []
    images: list[TranslatedImage] = []
    image_refs: list[str] = []
    next_attachment_index = attachment_offset
    next_ref_index = 1
    for part in content:
        if part.type in {"text", "input_text", "output_text"}:
            text_parts.append(part.text or "")
            continue
        if part.type in {"image_url", "input_image"}:
            if not allow_images:
                continue
            raw_image_url = part.image_url.url if hasattr(part.image_url, "url") else part.image_url
            if not isinstance(raw_image_url, str) or not raw_image_url:
                continue
            try:
                image = _parse_image_data_url(raw_image_url, image_index=next_attachment_index)
                images.append(image)
                image_refs.append(f"[Image {next_ref_index}]")
                next_attachment_index += 1
                next_ref_index += 1
            except ValueError:
                continue
            continue
        if part.type in {"file", "input_file"}:
            continue

    return "".join(text_parts), images, image_refs


def _render_message_text(text: str, image_refs: list[str]) -> str:
    text = text.strip()
    if not image_refs:
        return text
    attachment_text = "Attached images for this message: " + ", ".join(image_refs)
    if not text:
        return attachment_text
    return f"{text}\n\n{attachment_text}"


def _render_assistant_history_text(
    text: str,
    tool_calls: list[OpenAIToolCall] | None,
) -> str:
    if not tool_calls:
        return text.strip()
    return canonicalize_assistant_turn(
        text,
        [
            {
                "type": "function_call",
                "call_id": tool_call.id,
                "name": tool_call.function.name,
                "arguments": tool_call.function.arguments,
            }
            for tool_call in tool_calls
        ],
    )


def _render_tool_history_text(
    tool_call_id: str | None,
    name: str | None,
    content: str | list[ContentPart] | None,
) -> str:
    if isinstance(content, list):
        text = flatten_content(content, context="tool message")
    else:
        text = content or ""
    return canonicalize_tool_output(tool_call_id, name, text)


def _append_history_turn(
    prior_turns: list[HistoryTurn],
    transcript_lines: list[str],
    role: str,
    text: str,
) -> None:
    prior_turns.append(HistoryTurn(role=role, text=text))
    transcript_lines.append(f"{role.capitalize()}: {text}")


def _stringify_tool_output(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                item_type = str(item.get("type", ""))
                if item_type in {"text", "input_text", "output_text"}:
                    parts.append(str(item.get("text", "")))
                elif "output" in item:
                    parts.append(_stringify_tool_output(item.get("output")))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False, sort_keys=True))
            else:
                parts.append(str(item))
        return "".join(parts)
    if isinstance(value, dict):
        if "text" in value and isinstance(value["text"], str):
            return value["text"]
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def translate_openai_request(request: OpenAIChatRequest) -> TranslatedRequest:
    system_lines: list[str] = []
    prior_turns: list[HistoryTurn] = []
    transcript_lines: list[str] = []
    prompt = ""
    images: list[TranslatedImage] = []
    current_images: list[TranslatedImage] = []

    for index, message in enumerate(request.messages):
        is_last = index == len(request.messages) - 1
        text, message_images, image_refs = _extract_openai_content(
            message.content,
            allow_images=message.role == "user",
            attachment_offset=len(images) + 1,
        )
        rendered_text = _render_message_text(text, image_refs)
        if message.role == "assistant":
            rendered_text = _render_assistant_history_text(rendered_text, message.tool_calls)
        elif message.role == "tool":
            rendered_text = _render_tool_history_text(
                message.tool_call_id,
                message.name,
                message.content,
            )
        if not rendered_text and not message_images:
            continue
        images.extend(message_images)
        if message.role in {"system", "developer"}:
            system_lines.append(rendered_text)
            continue
        if is_last:
            if message.role != "user":
                raise ValueError("The final OpenAI message must be a user message.")
            prompt = rendered_text
            current_images = list(message_images)
            continue
        _append_history_turn(prior_turns, transcript_lines, message.role, rendered_text)

    additional_context: list[str] = []
    system_text = _join_lines(system_lines)
    if system_text:
        additional_context.append(f"System instructions:\n{system_text}")
    transcript_text = _join_lines(transcript_lines)
    if transcript_text:
        additional_context.append(f"Prior conversation transcript:\n{transcript_text}")
    return TranslatedRequest(
        prompt=prompt,
        additional_context=additional_context,
        attachments=images,
        current_attachments=current_images,
        system_text=system_text,
        prior_turns=prior_turns,
    )


def translate_responses_request(request: OpenAIResponsesRequest) -> TranslatedRequest:
    instructions = request.instructions or ""
    if isinstance(request.input, str):
        return TranslatedRequest(
            prompt=request.input,
            additional_context=[f"System instructions:\n{instructions}"] if instructions else [],
            system_text=instructions.strip(),
        )

    system_lines: list[str] = []
    if instructions:
        system_lines.append(instructions)
    prior_turns: list[HistoryTurn] = []
    transcript_lines: list[str] = []
    prompt = ""
    images: list[TranslatedImage] = []
    current_images: list[TranslatedImage] = []
    normalized_entries: list[tuple[str, str, list[TranslatedImage]]] = []
    pending_assistant_text = ""
    pending_assistant_tool_calls: list[dict[str, str]] = []

    def flush_pending_assistant() -> None:
        nonlocal pending_assistant_text, pending_assistant_tool_calls
        if not pending_assistant_text and not pending_assistant_tool_calls:
            return
        normalized_entries.append(
            (
                "assistant",
                canonicalize_assistant_turn(
                    pending_assistant_text,
                    pending_assistant_tool_calls,
                ),
                [],
            )
        )
        pending_assistant_text = ""
        pending_assistant_tool_calls = []

    items = request.input
    for item in items:
        if not isinstance(item, dict):
            flush_pending_assistant()
            normalized_entries.append(("user", str(item), []))
            continue

        item_type = str(item.get("type", ""))
        if item_type == "function_call":
            pending_assistant_tool_calls.append(
                {
                    "type": "function_call",
                    "call_id": str(item.get("call_id", "")),
                    "name": str(item.get("name", "")),
                    "arguments": str(item.get("arguments", "")),
                }
            )
            continue
        if item_type == "function_call_output":
            flush_pending_assistant()
            normalized_entries.append(
                (
                    "tool",
                    canonicalize_tool_output(
                        str(item.get("call_id", "")),
                        str(item.get("name", "")),
                        _stringify_tool_output(item.get("output")),
                    ),
                    [],
                )
            )
            continue

        role = str(item.get("role", ""))
        content = item.get("content", "") if "content" in item else ""
        if isinstance(content, list):
            parts = [
                ContentPart.model_validate(part)
                if isinstance(part, dict)
                else ContentPart(type="text", text=str(part))
                for part in content
            ]
            text, item_images, image_refs = _extract_openai_content(
                parts,
                allow_images=role == "user",
                attachment_offset=len(images) + 1,
            )
        else:
            text, item_images, image_refs = (str(content), [], [])
        rendered_text = _render_message_text(text, image_refs)
        if role == "assistant":
            flush_pending_assistant()
            pending_assistant_text = rendered_text
            images.extend(item_images)
            continue
        flush_pending_assistant()
        if not rendered_text and not item_images:
            if role == "user":
                normalized_entries.append((role, "", []))
            continue
        images.extend(item_images)
        normalized_entries.append((role, rendered_text, item_images))

    flush_pending_assistant()

    last_index = -1
    for index, (role, _rendered_text, _item_images) in enumerate(normalized_entries):
        if role not in {"system", "developer"}:
            last_index = index
    if last_index < 0 or normalized_entries[last_index][0] != "user":
        raise ValueError("The final OpenAI input item must be a user message.")

    for index, (role, rendered_text, item_images) in enumerate(normalized_entries):
        if role in {"system", "developer"}:
            if rendered_text:
                system_lines.append(rendered_text)
            continue
        if index == last_index:
            prompt = rendered_text
            current_images = list(item_images)
            continue
        _append_history_turn(prior_turns, transcript_lines, role, rendered_text)
    additional_context: list[str] = []
    system_text = _join_lines(system_lines)
    if system_text:
        additional_context.append(f"System instructions:\n{system_text}")
    transcript_text = _join_lines(transcript_lines)
    if transcript_text:
        additional_context.append(f"Prior conversation transcript:\n{transcript_text}")
    return TranslatedRequest(
        prompt=prompt,
        additional_context=additional_context,
        attachments=images,
        current_attachments=current_images,
        system_text=system_text,
        prior_turns=prior_turns,
    )


def translate_anthropic_request(
    request: AnthropicMessagesRequest,
) -> TranslatedRequest:
    system_text = flatten_content(request.system, context="Anthropic system prompt").strip()
    prior_turns: list[HistoryTurn] = []
    transcript_lines: list[str] = []
    prompt = ""

    for index, message in enumerate(request.messages):
        text = flatten_content(
            message.content,
            context=f"Anthropic message {index + 1}",
        ).strip()
        is_last = index == len(request.messages) - 1
        if is_last:
            if message.role != "user":
                raise ValueError("The final Anthropic message must be a user message.")
            prompt = text
            continue
        if not text:
            continue
        prior_turns.append(HistoryTurn(role=message.role, text=text))
        transcript_lines.append(f"{message.role.capitalize()}: {text}")

    additional_context: list[str] = []
    if system_text:
        additional_context.append(f"System instructions:\n{system_text}")
    transcript_text = _join_lines(transcript_lines)
    if transcript_text:
        additional_context.append(f"Prior conversation transcript:\n{transcript_text}")
    return TranslatedRequest(
        prompt=prompt,
        additional_context=additional_context,
        system_text=system_text,
        prior_turns=prior_turns,
    )
