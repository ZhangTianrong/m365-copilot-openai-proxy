from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .copilot_models import resolve_copilot_model_transport
from .conversation_reuse import ConversationReuseService, PreparedConversationTurn
from .config import Settings
from .substrate_client import (
    SubstrateCopilotClient,
    SubstrateCopilotError,
)
from .token_store import TokenStoreError, load_access_token
from .models import AnthropicMessagesRequest, OpenAIChatRequest, OpenAIResponsesRequest
from .translator import (
    translate_anthropic_request,
    translate_openai_request,
    translate_responses_request,
)

logger = logging.getLogger(__name__)


def _ensure_debug_handler() -> None:
    for handler in logger.handlers:
        if getattr(handler, "_m365_debug_handler", False):
            return
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    setattr(handler, "_m365_debug_handler", True)
    logger.addHandler(handler)


def create_app(
    settings: Settings | None = None,
    copilot_client_factory: Callable[[], SubstrateCopilotClient] | None = None,
) -> FastAPI:
    app = FastAPI(title="Microsoft 365 Copilot OpenAI Proxy")
    resolved_settings = settings or Settings()
    model_transport, model_warning = resolve_copilot_model_transport(
        resolved_settings.copilot_model_name
    )
    if model_warning:
        logger.warning(model_warning)
    if resolved_settings.debug_logging:
        logger.setLevel(logging.INFO)
        _ensure_debug_handler()
        _emit_debug_log(
            resolved_settings,
            "debug.enabled",
            conversation_reuse_enabled=resolved_settings.enable_conversation_reuse,
            copilot_model_name=model_transport.visible_name if model_transport else "Auto",
        )
    app.state.settings = resolved_settings
    app.state.conversation_reuse_service = ConversationReuseService(resolved_settings)
    app.state.copilot_client_factory = copilot_client_factory or (
        lambda: SubstrateCopilotClient(
            load_access_token(resolved_settings),
            resolved_settings.time_zone,
            model_transport=model_transport,
        )
    )

    def get_settings() -> Settings:
        return app.state.settings

    def get_copilot_client() -> SubstrateCopilotClient:
        try:
            return app.state.copilot_client_factory()
        except (SubstrateCopilotError, TokenStoreError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    def get_conversation_reuse_service() -> ConversationReuseService:
        return app.state.conversation_reuse_service

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/models")
    async def list_models(settings: Settings = Depends(get_settings)) -> dict:
        return {
            "object": "list",
            "data": [
                {
                    "id": settings.model_alias,
                    "object": "model",
                    "owned_by": "microsoft-365-copilot",
                }
            ],
        }

    @app.post("/v1/chat/completions")
    async def chat_completions(
        raw: Request,
        request: OpenAIChatRequest,
        settings: Settings = Depends(get_settings),
        client: SubstrateCopilotClient = Depends(get_copilot_client),
        conversation_reuse: ConversationReuseService = Depends(get_conversation_reuse_service),
    ):
        try:
            raw_body = await raw.json()
            _log_request_debug(settings, "chat.completions", raw_body)
            translated = translate_openai_request(request)
            turn = conversation_reuse.prepare_turn(
                translated,
                scope_user=request.user,
            )
            _log_translation_debug(settings, "chat.completions", translated, turn)
            if request.stream:
                return StreamingResponse(
                    _openai_stream(
                        settings.model_alias,
                        settings,
                        client,
                        conversation_reuse,
                        turn,
                    ),
                    media_type="text/event-stream",
                )
            text = await client.chat(
                turn.prompt,
                turn.additional_context,
                turn.images,
                conversation_id=turn.conversation_id,
                is_start_of_session=turn.is_start_of_session,
            )
            conversation_reuse.complete_turn(turn, text)
            _emit_debug_log(
                settings,
                "turn.completed",
                endpoint="chat.completions",
                conversation_id=turn.conversation_id,
                routing_mode=turn.routing_mode,
                assistant_preview=_sanitize_debug_value(text),
                assistant_length=len(text),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except SubstrateCopilotError as exc:
            if "turn" in locals():
                conversation_reuse.abort_turn(turn)
                _emit_debug_log(
                    settings,
                    "turn.aborted",
                    endpoint="chat.completions",
                    conversation_id=turn.conversation_id,
                    routing_mode=turn.routing_mode,
                    error=str(exc),
                )
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        return JSONResponse({
            "id": f"chatcmpl_{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": settings.model_alias,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
        })

    @app.post("/v1/responses")
    async def openai_responses(
        raw: Request,
        settings: Settings = Depends(get_settings),
        client: SubstrateCopilotClient = Depends(get_copilot_client),
        conversation_reuse: ConversationReuseService = Depends(get_conversation_reuse_service),
    ):
        body = await raw.json()
        try:
            _log_request_debug(settings, "responses", body)
            request = OpenAIResponsesRequest.model_validate(body)
            translated = translate_responses_request(request)
            turn = conversation_reuse.prepare_turn(translated, scope_user=None)
            _log_translation_debug(settings, "responses", translated, turn)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if request.stream:
            return StreamingResponse(
                _responses_stream(
                    settings.model_alias,
                    settings,
                    client,
                    conversation_reuse,
                    turn,
                ),
                media_type="text/event-stream",
            )

        try:
            text = await client.chat(
                turn.prompt,
                turn.additional_context,
                turn.images,
                conversation_id=turn.conversation_id,
                is_start_of_session=turn.is_start_of_session,
            )
            conversation_reuse.complete_turn(turn, text)
            _emit_debug_log(
                settings,
                "turn.completed",
                endpoint="responses",
                conversation_id=turn.conversation_id,
                routing_mode=turn.routing_mode,
                assistant_preview=_sanitize_debug_value(text),
                assistant_length=len(text),
            )
        except SubstrateCopilotError as exc:
            conversation_reuse.abort_turn(turn)
            _emit_debug_log(
                settings,
                "turn.aborted",
                endpoint="responses",
                conversation_id=turn.conversation_id,
                routing_mode=turn.routing_mode,
                error=str(exc),
            )
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        return JSONResponse({
            "id": f"resp_{uuid.uuid4().hex}",
            "object": "response",
            "created_at": int(time.time()),
            "model": settings.model_alias,
            "output": [{
                "type": "message",
                "id": f"msg_{uuid.uuid4().hex}",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }],
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        })

    @app.post("/v1/messages")
    async def anthropic_messages(
        raw: Request,
        request: AnthropicMessagesRequest,
        settings: Settings = Depends(get_settings),
        client: SubstrateCopilotClient = Depends(get_copilot_client),
        conversation_reuse: ConversationReuseService = Depends(get_conversation_reuse_service),
    ):
        try:
            raw_body = await raw.json()
            _log_request_debug(settings, "messages", raw_body)
            translated = translate_anthropic_request(request)
            turn = conversation_reuse.prepare_turn(translated, scope_user=None)
            _log_translation_debug(settings, "messages", translated, turn)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if request.stream:
            return StreamingResponse(
                _anthropic_stream(settings.model_alias, settings, client, conversation_reuse, turn),
                media_type="text/event-stream",
            )

        try:
            text = await client.chat(
                turn.prompt,
                turn.additional_context,
                conversation_id=turn.conversation_id,
                is_start_of_session=turn.is_start_of_session,
            )
            conversation_reuse.complete_turn(turn, text)
            _emit_debug_log(
                settings,
                "turn.completed",
                endpoint="messages",
                conversation_id=turn.conversation_id,
                routing_mode=turn.routing_mode,
                assistant_preview=_sanitize_debug_value(text),
                assistant_length=len(text),
            )
        except SubstrateCopilotError as exc:
            conversation_reuse.abort_turn(turn)
            _emit_debug_log(
                settings,
                "turn.aborted",
                endpoint="messages",
                conversation_id=turn.conversation_id,
                routing_mode=turn.routing_mode,
                error=str(exc),
            )
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        return JSONResponse({
            "id": f"msg_{uuid.uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "model": settings.model_alias,
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        })

    return app


def _should_log_debug(settings: Settings) -> bool:
    return settings.debug_logging


def _truncate_debug_string(value: str, limit: int = 500) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}...<truncated {len(value) - limit} chars>"


def _sanitize_debug_value(value: Any) -> Any:
    if isinstance(value, str):
        if value.startswith("data:image/") and ";base64," in value:
            prefix, encoded = value.split(",", 1)
            mime_type = prefix[5:].split(";", 1)[0]
            return {
                "type": "data_url_image",
                "mime_type": mime_type,
                "base64_length": len(encoded),
            }
        return _truncate_debug_string(value)
    if isinstance(value, dict):
        return {str(key): _sanitize_debug_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_debug_value(item) for item in value]
    return value


def _collect_interesting_request_fields(value: Any, path: str = "") -> dict[str, Any]:
    matches: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}" if path else key_text
            if key_text in {"messages", "input", "instructions", "model", "user"}:
                matches[child_path] = _sanitize_debug_value(item)
            matches.update(_collect_interesting_request_fields(item, child_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            child_path = f"{path}[{index}]" if path else f"[{index}]"
            matches.update(_collect_interesting_request_fields(item, child_path))
    return matches


def _emit_debug_log(settings: Settings, event: str, **payload: Any) -> None:
    if not _should_log_debug(settings):
        return
    logger.info(
        "m365-debug %s",
        json.dumps(
            {"event": event, **payload},
            ensure_ascii=False,
            sort_keys=True,
        ),
    )


def _log_request_debug(settings: Settings, endpoint: str, raw_body: Any) -> None:
    _emit_debug_log(
        settings,
        "request.received",
        endpoint=endpoint,
        body=_sanitize_debug_value(raw_body),
        ignored_fields=_collect_interesting_request_fields(raw_body),
    )


def _log_translation_debug(
    settings: Settings,
    endpoint: str,
    translated,
    turn: PreparedConversationTurn,
) -> None:
    _emit_debug_log(
        settings,
        "turn.prepared",
        endpoint=endpoint,
        conversation_id=turn.conversation_id,
        is_start_of_session=turn.is_start_of_session,
        routing_mode=turn.routing_mode,
        reuse_enabled=turn.routing_mode != "stateless_disabled",
        prompt=_sanitize_debug_value(translated.prompt),
        additional_context=_sanitize_debug_value(translated.additional_context),
        system_text=_sanitize_debug_value(translated.system_text),
        prior_turn_count=len(translated.prior_turns),
        prior_turns=[
            {"role": prior_turn.role, "text": _sanitize_debug_value(prior_turn.text)}
            for prior_turn in translated.prior_turns
        ],
        image_filenames=[image.filename for image in translated.images],
        current_image_filenames=[image.filename for image in translated.current_images],
    )


async def _openai_stream(
    model_alias: str,
    settings: Settings,
    client: SubstrateCopilotClient,
    conversation_reuse: ConversationReuseService,
    turn: PreparedConversationTurn,
) -> AsyncIterator[str]:
    completion_id = f"chatcmpl_{uuid.uuid4().hex}"
    created = int(time.time())
    first_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model_alias,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }
    yield f"data: {json.dumps(first_chunk)}\n\n"
    full_text = ""
    try:
        async for delta in client.chat_stream(
            turn.prompt,
            turn.additional_context,
            turn.images,
            conversation_id=turn.conversation_id,
            is_start_of_session=turn.is_start_of_session,
        ):
            full_text += delta
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_alias,
                "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
    except Exception as exc:
        conversation_reuse.abort_turn(turn)
        _emit_debug_log(
            settings,
            "turn.aborted",
            endpoint="chat.completions",
            conversation_id=turn.conversation_id,
            routing_mode=turn.routing_mode,
            error=str(exc),
        )
        raise
    final_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model_alias,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    conversation_reuse.complete_turn(turn, full_text)
    _emit_debug_log(
        settings,
        "turn.completed",
        endpoint="chat.completions",
        conversation_id=turn.conversation_id,
        routing_mode=turn.routing_mode,
        assistant_preview=_sanitize_debug_value(full_text),
        assistant_length=len(full_text),
    )
    yield f"data: {json.dumps(final_chunk)}\n\n"
    yield "data: [DONE]\n\n"


async def _responses_stream(
    model_alias: str,
    settings: Settings,
    client: SubstrateCopilotClient,
    conversation_reuse: ConversationReuseService,
    turn: PreparedConversationTurn,
) -> AsyncIterator[str]:
    resp_id = f"resp_{uuid.uuid4().hex}"
    item_id = f"msg_{uuid.uuid4().hex}"
    created = int(time.time())

    yield f"data: {json.dumps({'type': 'response.created', 'response': {'id': resp_id, 'object': 'response', 'created_at': created, 'model': model_alias, 'status': 'in_progress', 'output': []}})}\n\n"
    yield f"data: {json.dumps({'type': 'response.output_item.added', 'output_index': 0, 'item': {'id': item_id, 'type': 'message', 'role': 'assistant', 'content': []}})}\n\n"
    yield f"data: {json.dumps({'type': 'response.content_part.added', 'item_id': item_id, 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': ''}})}\n\n"

    full_text = ""
    try:
        async for delta in client.chat_stream(
            turn.prompt,
            turn.additional_context,
            turn.images,
            conversation_id=turn.conversation_id,
            is_start_of_session=turn.is_start_of_session,
        ):
            full_text += delta
            yield f"data: {json.dumps({'type': 'response.output_text.delta', 'item_id': item_id, 'output_index': 0, 'content_index': 0, 'delta': delta})}\n\n"
    except Exception as exc:
        conversation_reuse.abort_turn(turn)
        _emit_debug_log(
            settings,
            "turn.aborted",
            endpoint="responses",
            conversation_id=turn.conversation_id,
            routing_mode=turn.routing_mode,
            error=str(exc),
        )
        raise

    yield f"data: {json.dumps({'type': 'response.output_text.done', 'item_id': item_id, 'output_index': 0, 'content_index': 0, 'text': full_text})}\n\n"
    conversation_reuse.complete_turn(turn, full_text)
    _emit_debug_log(
        settings,
        "turn.completed",
        endpoint="responses",
        conversation_id=turn.conversation_id,
        routing_mode=turn.routing_mode,
        assistant_preview=_sanitize_debug_value(full_text),
        assistant_length=len(full_text),
    )
    yield f"data: {json.dumps({'type': 'response.completed', 'response': {'id': resp_id, 'object': 'response', 'created_at': created, 'model': model_alias, 'status': 'completed', 'output': [{'id': item_id, 'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': full_text}]}], 'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}}})}\n\n"


async def _anthropic_stream(
    model_alias: str,
    settings: Settings,
    client: SubstrateCopilotClient,
    conversation_reuse: ConversationReuseService,
    turn: PreparedConversationTurn,
) -> AsyncIterator[str]:
    msg_id = f"msg_{uuid.uuid4().hex}"

    def sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n"

    yield sse("message_start", {"type": "message_start", "message": {"id": msg_id, "type": "message", "role": "assistant", "content": [], "model": model_alias, "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}})
    yield sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
    yield sse("ping", {"type": "ping"})

    full_text = ""
    try:
        async for delta in client.chat_stream(
            turn.prompt,
            turn.additional_context,
            conversation_id=turn.conversation_id,
            is_start_of_session=turn.is_start_of_session,
        ):
            full_text += delta
            yield sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": delta}})
    except Exception as exc:
        conversation_reuse.abort_turn(turn)
        _emit_debug_log(
            settings,
            "turn.aborted",
            endpoint="messages",
            conversation_id=turn.conversation_id,
            routing_mode=turn.routing_mode,
            error=str(exc),
        )
        raise

    yield sse("content_block_stop", {"type": "content_block_stop", "index": 0})
    yield sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 0}})
    conversation_reuse.complete_turn(turn, full_text)
    _emit_debug_log(
        settings,
        "turn.completed",
        endpoint="messages",
        conversation_id=turn.conversation_id,
        routing_mode=turn.routing_mode,
        assistant_preview=_sanitize_debug_value(full_text),
        assistant_length=len(full_text),
    )
    yield sse("message_stop", {"type": "message_stop"})
