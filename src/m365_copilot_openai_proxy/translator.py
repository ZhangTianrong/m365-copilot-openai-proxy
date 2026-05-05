from __future__ import annotations

import base64
import binascii
import re
from typing import Iterable

from .models import (
    AnthropicMessagesRequest,
    ContentPart,
    HistoryTurn,
    OpenAIChatRequest,
    OpenAIResponsesRequest,
    TranslatedAttachment,
    TranslatedRequest,
)

_SUPPORTED_IMAGE_MIME_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
}
_SUPPORTED_FILE_MIME_TYPES = {
    "application/pdf": "pdf",
    "text/plain": "txt",
    "text/markdown": "md",
    "application/json": "json",
}
_DATA_URL_RE = re.compile(r"^data:(?P<mime>[-\w.+/]+);base64,(?P<data>.+)$", re.DOTALL)


def flatten_content(content: str | list[ContentPart] | None, *, context: str) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    text_parts: list[str] = []
    for part in content:
        if part.type == "text":
            text_parts.append(part.text or "")
    return "".join(text_parts)


def _join_lines(lines: Iterable[str]) -> str:
    return "\n".join(line for line in lines if line).strip()


def _parse_data_url(
    data_url: str,
    *,
    expected_kind: str,
    attachment_index: int,
    filename: str | None = None,
) -> TranslatedAttachment:
    match = _DATA_URL_RE.match(data_url)
    if not match:
        if data_url.startswith(("http://", "https://")):
            raise ValueError(f"Remote {expected_kind} URLs are not supported. Use a data URL instead.")
        raise ValueError(f"{expected_kind.capitalize()} attachments must use a base64 data URL.")

    mime_type = match.group("mime").lower()
    if expected_kind == "image":
        file_extension = _SUPPORTED_IMAGE_MIME_TYPES.get(mime_type)
        if not file_extension:
            raise ValueError(
                "Unsupported image MIME type. Only PNG, JPEG, and WebP data URLs are supported."
            )
        default_filename = (
            f"image.{file_extension}"
            if attachment_index == 1
            else f"image-{attachment_index}.{file_extension}"
        )
    else:
        file_extension = _SUPPORTED_FILE_MIME_TYPES.get(mime_type)
        if not file_extension:
            raise ValueError(
                "Unsupported file MIME type. Only PDF, plain text, Markdown, and JSON data URLs are supported."
            )
        default_filename = (
            f"file.{file_extension}"
            if attachment_index == 1
            else f"file-{attachment_index}.{file_extension}"
        )

    encoded = match.group("data")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{expected_kind.capitalize()} attachment data URL is not valid base64.") from exc

    resolved_filename = (filename or "").strip() or default_filename
    return TranslatedAttachment(
        kind=expected_kind,
        filename=resolved_filename,
        mime_type=mime_type,
        file_extension=file_extension,
        data_url=data_url,
        content=content,
    )


def _parse_image_data_url(data_url: str, *, image_index: int) -> TranslatedAttachment:
    return _parse_data_url(data_url, expected_kind="image", attachment_index=image_index)


def _parse_file_data_url(
    data_url: str,
    *,
    file_index: int,
    filename: str | None = None,
) -> TranslatedAttachment:
    return _parse_data_url(
        data_url,
        expected_kind="file",
        attachment_index=file_index,
        filename=filename,
    )


def _extract_openai_content(
    content: str | list[ContentPart] | None,
    *,
    allow_images: bool,
    allow_files: bool,
    attachment_offset: int,
) -> tuple[str, list[TranslatedAttachment], list[str], list[str]]:
    if content is None:
        return "", [], [], []
    if isinstance(content, str):
        return content, [], [], []

    text_parts: list[str] = []
    attachments: list[TranslatedAttachment] = []
    image_refs: list[str] = []
    file_refs: list[str] = []
    next_attachment_index = attachment_offset
    for part in content:
        if part.type in {"text", "input_text"}:
            text_parts.append(part.text or "")
            continue
        if part.type in {"image_url", "input_image"}:
            if not allow_images:
                continue
            raw_image_url = part.image_url.url if hasattr(part.image_url, "url") else part.image_url
            if not isinstance(raw_image_url, str) or not raw_image_url:
                continue
            try:
                attachment = _parse_image_data_url(raw_image_url, image_index=next_attachment_index)
                attachments.append(attachment)
                image_refs.append(f"[Image {next_attachment_index}]")
                next_attachment_index += 1
            except ValueError:
                continue
            continue
        if part.type in {"file", "input_file"}:
            if not allow_files:
                continue
            raw_file = part.file
            raw_file_data = None
            raw_filename = part.filename
            if raw_file is not None:
                raw_file_data = raw_file.file_data or raw_file.file_url
                raw_filename = raw_filename or raw_file.filename
            if raw_file_data is None:
                raw_file_data = part.file_data or part.file_url
            if not isinstance(raw_file_data, str) or not raw_file_data:
                continue
            try:
                attachment = _parse_file_data_url(
                    raw_file_data,
                    file_index=next_attachment_index,
                    filename=raw_filename,
                )
                attachments.append(attachment)
                file_refs.append(f"[File {next_attachment_index}: {attachment.filename}]")
                next_attachment_index += 1
            except ValueError:
                continue
            continue

    return "".join(text_parts), attachments, image_refs, file_refs


def _render_message_text(text: str, image_refs: list[str], file_refs: list[str]) -> str:
    text = text.strip()
    attachment_lines: list[str] = []
    if image_refs:
        attachment_lines.append("Attached images for this message: " + ", ".join(image_refs))
    if file_refs:
        attachment_lines.append("Attached files for this message: " + ", ".join(file_refs))
    if not attachment_lines:
        return text
    attachment_text = "\n".join(attachment_lines)
    if not text:
        return attachment_text
    return f"{text}\n\n{attachment_text}"


def translate_openai_request(request: OpenAIChatRequest) -> TranslatedRequest:
    system_lines: list[str] = []
    prior_turns: list[HistoryTurn] = []
    transcript_lines: list[str] = []
    prompt = ""
    attachments: list[TranslatedAttachment] = []
    current_attachments: list[TranslatedAttachment] = []

    for index, message in enumerate(request.messages):
        is_last = index == len(request.messages) - 1
        text, message_attachments, image_refs, file_refs = _extract_openai_content(
            message.content,
            allow_images=message.role == "user",
            allow_files=message.role == "user",
            attachment_offset=len(attachments) + 1,
        )
        rendered_text = _render_message_text(text, image_refs, file_refs)
        if not rendered_text and not message_attachments:
            continue
        attachments.extend(message_attachments)
        if message.role in {"system", "developer"}:
            system_lines.append(rendered_text)
            continue
        if is_last:
            if message.role != "user":
                raise ValueError("The final OpenAI message must be a user message.")
            prompt = rendered_text
            current_attachments = list(message_attachments)
            continue
        prior_turns.append(HistoryTurn(role=message.role, text=rendered_text))
        transcript_lines.append(f"{message.role.capitalize()}: {rendered_text}")

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
        attachments=attachments,
        current_attachments=current_attachments,
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
    attachments: list[TranslatedAttachment] = []
    current_attachments: list[TranslatedAttachment] = []
    items = request.input
    for index, item in enumerate(items):
        role = item.get("role", "") if isinstance(item, dict) else ""
        content = item.get("content", "") if isinstance(item, dict) else str(item)
        is_last = index == len(items) - 1
        if isinstance(content, list):
            parts = [ContentPart.model_validate(part) if isinstance(part, dict) else ContentPart(type="text", text=str(part)) for part in content]
            text, item_attachments, image_refs, file_refs = _extract_openai_content(
                parts,
                allow_images=role == "user",
                allow_files=role == "user",
                attachment_offset=len(attachments) + 1,
            )
        else:
            text, item_attachments, image_refs, file_refs = (content, [], [], [])
        rendered_text = _render_message_text(text, image_refs, file_refs)
        if not rendered_text and not item_attachments:
            continue
        attachments.extend(item_attachments)
        if role in {"system", "developer"}:
            system_lines.append(rendered_text)
            continue
        if is_last:
            if role != "user":
                raise ValueError("The final OpenAI input item must be a user message.")
            prompt = rendered_text
            current_attachments = list(item_attachments)
            continue
        prior_turns.append(HistoryTurn(role=role, text=rendered_text))
        transcript_lines.append(f"{role.capitalize()}: {rendered_text}")
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
        attachments=attachments,
        current_attachments=current_attachments,
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
