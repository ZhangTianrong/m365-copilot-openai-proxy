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
from .token_store import TokenStoreError, load_auth_session
from .models import (
    AnthropicMessagesRequest,
    ConversationTransportState,
    OpenAIChatRequest,
    OpenAIResponsesRequest,
)
from .proxy_profiles import (
    AssistantPostprocessResult,
    ProxyProfile,
    list_public_model_ids,
    resolve_proxy_profile,
)
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
            load_auth_session(resolved_settings),
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
                    "id": model_id,
                    "object": "model",
                    "owned_by": "microsoft-365-copilot",
                }
                for model_id in list_public_model_ids(settings.model_alias)
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
            profile = resolve_proxy_profile(request.model, settings.model_alias)
            translated = profile.preprocess_translated(translate_openai_request(request))
            turn = conversation_reuse.prepare_turn(
                translated,
                scope_user=request.user,
            )
            _log_translation_debug(settings, "chat.completions", translated, turn)
            if request.stream:
                return StreamingResponse(
                    _openai_stream(
                        profile,
                        settings,
                        client,
                        conversation_reuse,
                        turn,
                    ),
                    media_type="text/event-stream",
                )
            transport_state = ConversationTransportState()
            text = await client.chat(
                turn.prompt,
                turn.additional_context,
                turn.attachments,
                conversation_id=turn.conversation_id,
                transport_session_id=turn.transport_session_id,
                is_start_of_session=turn.is_start_of_session,
                transport_state=transport_state,
            )
            assistant = profile.postprocess_assistant_text(text)
            conversation_reuse.complete_turn(
                turn,
                assistant.history_text,
                resolved_conversation_id=transport_state.conversation_id,
                resolved_transport_session_id=transport_state.session_id,
            )
            _emit_debug_log(
                settings,
                "turn.completed",
                endpoint="chat.completions",
                conversation_id=turn.conversation_id,
                routing_mode=turn.routing_mode,
                assistant_preview=_sanitize_debug_value(text),
                assistant_length=len(text),
                public_model=profile.public_model_id,
                tool_call_count=len(assistant.tool_calls),
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
            "model": profile.public_model_id,
            "choices": [
                _build_chat_completion_choice(assistant)
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
            profile = resolve_proxy_profile(request.model, settings.model_alias)
            translated = profile.preprocess_translated(translate_responses_request(request))
            turn = conversation_reuse.prepare_turn(translated, scope_user=None)
            _log_translation_debug(settings, "responses", translated, turn)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if request.stream:
            return StreamingResponse(
                _responses_stream(
                    profile,
                    settings,
                    client,
                    conversation_reuse,
                    turn,
                ),
                media_type="text/event-stream",
            )

        try:
            transport_state = ConversationTransportState()
            text = await client.chat(
                turn.prompt,
                turn.additional_context,
                turn.attachments,
                conversation_id=turn.conversation_id,
                transport_session_id=turn.transport_session_id,
                is_start_of_session=turn.is_start_of_session,
                transport_state=transport_state,
            )
            assistant = profile.postprocess_assistant_text(text)
            conversation_reuse.complete_turn(
                turn,
                assistant.history_text,
                resolved_conversation_id=transport_state.conversation_id,
                resolved_transport_session_id=transport_state.session_id,
            )
            _emit_debug_log(
                settings,
                "turn.completed",
                endpoint="responses",
                conversation_id=turn.conversation_id,
                routing_mode=turn.routing_mode,
                assistant_preview=_sanitize_debug_value(text),
                assistant_length=len(text),
                public_model=profile.public_model_id,
                tool_call_count=len(assistant.tool_calls),
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
            "model": profile.public_model_id,
            "output": _build_responses_output(assistant),
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
            transport_state = ConversationTransportState()
            text = await client.chat(
                turn.prompt,
                turn.additional_context,
                conversation_id=turn.conversation_id,
                transport_session_id=turn.transport_session_id,
                is_start_of_session=turn.is_start_of_session,
                transport_state=transport_state,
            )
            conversation_reuse.complete_turn(
                turn,
                text,
                resolved_conversation_id=transport_state.conversation_id,
                resolved_transport_session_id=transport_state.session_id,
            )
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
        if value.startswith("data:") and ";base64," in value:
            prefix, encoded = value.split(",", 1)
            mime_type = prefix[5:].split(";", 1)[0]
            return {
                "type": "data_url",
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
        transport_session_id=turn.transport_session_id,
        is_start_of_session=turn.is_start_of_session,
        routing_mode=turn.routing_mode,
        reuse_enabled=turn.routing_mode != "stateless_disabled",
        prompt=_sanitize_debug_value(translated.prompt),
        additional_context=_sanitize_debug_value(translated.additional_context),
        transport_additional_context=_sanitize_debug_value(
            translated.transport_additional_context
        ),
        system_text=_sanitize_debug_value(translated.system_text),
        prior_turn_count=len(translated.prior_turns),
        prior_turns=[
            {"role": prior_turn.role, "text": _sanitize_debug_value(prior_turn.text)}
            for prior_turn in translated.prior_turns
        ],
        attachment_filenames=[attachment.filename for attachment in translated.attachments],
        current_attachment_filenames=[
            attachment.filename for attachment in translated.current_attachments
        ],
    )


def _build_chat_completion_choice(assistant: AssistantPostprocessResult) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": assistant.visible_text if assistant.has_tool_calls else assistant.raw_text,
    }
    if assistant.has_tool_calls:
        message["content"] = assistant.visible_text or None
        message["tool_calls"] = [
            tool_call.to_chat_tool_call()
            for tool_call in assistant.tool_calls
        ]
    return {
        "index": 0,
        "message": message,
        "finish_reason": "tool_calls" if assistant.has_tool_calls else "stop",
    }


def _build_responses_output(assistant: AssistantPostprocessResult) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    visible_text = assistant.visible_text if assistant.has_tool_calls else assistant.raw_text
    if visible_text:
        output.append(
            {
                "type": "message",
                "id": f"msg_{uuid.uuid4().hex}",
                "role": "assistant",
                "content": [{"type": "output_text", "text": visible_text}],
            }
        )
    if assistant.has_tool_calls:
        output.extend(tool_call.to_responses_item() for tool_call in assistant.tool_calls)
    return output


async def _collect_stream_text(
    client: SubstrateCopilotClient,
    turn: PreparedConversationTurn,
    transport_state: ConversationTransportState,
) -> str:
    full_text = ""
    async for delta in client.chat_stream(
        turn.prompt,
        turn.additional_context,
        turn.attachments,
        conversation_id=turn.conversation_id,
        transport_session_id=turn.transport_session_id,
        is_start_of_session=turn.is_start_of_session,
        transport_state=transport_state,
    ):
        full_text += delta
    return full_text


async def _openai_stream(
    profile: ProxyProfile,
    settings: Settings,
    client: SubstrateCopilotClient,
    conversation_reuse: ConversationReuseService,
    turn: PreparedConversationTurn,
) -> AsyncIterator[str]:
    completion_id = f"chatcmpl_{uuid.uuid4().hex}"
    created = int(time.time())
    transport_state = ConversationTransportState()
    try:
        if profile.buffered_streaming:
            full_text = await _collect_stream_text(client, turn, transport_state)
            assistant = profile.postprocess_assistant_text(full_text)
            yield f"data: {json.dumps({'id': completion_id, 'object': 'chat.completion.chunk', 'created': created, 'model': profile.public_model_id, 'choices': [{'index': 0, 'delta': {'role': 'assistant'}, 'finish_reason': None}]})}\n\n"
            if assistant.visible_text:
                yield f"data: {json.dumps({'id': completion_id, 'object': 'chat.completion.chunk', 'created': created, 'model': profile.public_model_id, 'choices': [{'index': 0, 'delta': {'content': assistant.visible_text}, 'finish_reason': None}]})}\n\n"
            for index, tool_call in enumerate(assistant.tool_calls):
                yield f"data: {json.dumps({'id': completion_id, 'object': 'chat.completion.chunk', 'created': created, 'model': profile.public_model_id, 'choices': [{'index': 0, 'delta': {'tool_calls': [tool_call.to_chat_tool_call(index=index)]}, 'finish_reason': None}]})}\n\n"
            final_reason = "tool_calls" if assistant.has_tool_calls else "stop"
            conversation_reuse.complete_turn(
                turn,
                assistant.history_text,
                resolved_conversation_id=transport_state.conversation_id,
                resolved_transport_session_id=transport_state.session_id,
            )
            _emit_debug_log(
                settings,
                "turn.completed",
                endpoint="chat.completions",
                conversation_id=turn.conversation_id,
                routing_mode=turn.routing_mode,
                assistant_preview=_sanitize_debug_value(full_text),
                assistant_length=len(full_text),
                public_model=profile.public_model_id,
                tool_call_count=len(assistant.tool_calls),
            )
            yield f"data: {json.dumps({'id': completion_id, 'object': 'chat.completion.chunk', 'created': created, 'model': profile.public_model_id, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': final_reason}]})}\n\n"
            yield "data: [DONE]\n\n"
            return

        first_chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": profile.public_model_id,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(first_chunk)}\n\n"
        full_text = ""
        async for delta in client.chat_stream(
            turn.prompt,
            turn.additional_context,
            turn.attachments,
            conversation_id=turn.conversation_id,
            transport_session_id=turn.transport_session_id,
            is_start_of_session=turn.is_start_of_session,
            transport_state=transport_state,
        ):
            full_text += delta
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": profile.public_model_id,
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
        "model": profile.public_model_id,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    conversation_reuse.complete_turn(
        turn,
        full_text,
        resolved_conversation_id=transport_state.conversation_id,
        resolved_transport_session_id=transport_state.session_id,
    )
    _emit_debug_log(
        settings,
        "turn.completed",
        endpoint="chat.completions",
        conversation_id=turn.conversation_id,
        routing_mode=turn.routing_mode,
        assistant_preview=_sanitize_debug_value(full_text),
        assistant_length=len(full_text),
        public_model=profile.public_model_id,
        tool_call_count=0,
    )
    yield f"data: {json.dumps(final_chunk)}\n\n"
    yield "data: [DONE]\n\n"


async def _responses_stream(
    profile: ProxyProfile,
    settings: Settings,
    client: SubstrateCopilotClient,
    conversation_reuse: ConversationReuseService,
    turn: PreparedConversationTurn,
) -> AsyncIterator[str]:
    resp_id = f"resp_{uuid.uuid4().hex}"
    created = int(time.time())
    transport_state = ConversationTransportState()
    try:
        if profile.buffered_streaming:
            full_text = await _collect_stream_text(client, turn, transport_state)
            assistant = profile.postprocess_assistant_text(full_text)
            output = _build_responses_output(assistant)
            yield f"data: {json.dumps({'type': 'response.created', 'response': {'id': resp_id, 'object': 'response', 'created_at': created, 'model': profile.public_model_id, 'status': 'in_progress', 'output': []}})}\n\n"
            for output_index, item in enumerate(output):
                yield f"data: {json.dumps({'type': 'response.output_item.added', 'output_index': output_index, 'item': item})}\n\n"
                if item["type"] == "message":
                    part = item["content"][0]
                    yield f"data: {json.dumps({'type': 'response.content_part.added', 'item_id': item['id'], 'output_index': output_index, 'content_index': 0, 'part': part})}\n\n"
                    yield f"data: {json.dumps({'type': 'response.output_text.delta', 'item_id': item['id'], 'output_index': output_index, 'content_index': 0, 'delta': part['text']})}\n\n"
                    yield f"data: {json.dumps({'type': 'response.output_text.done', 'item_id': item['id'], 'output_index': output_index, 'content_index': 0, 'text': part['text']})}\n\n"
                    yield f"data: {json.dumps({'type': 'response.output_item.done', 'output_index': output_index, 'item': item})}\n\n"
                elif item["type"] == "function_call":
                    item_id = item["id"]
                    yield f"data: {json.dumps({'type': 'response.function_call_arguments.delta', 'item_id': item_id, 'output_index': output_index, 'delta': item['arguments']})}\n\n"
                    yield f"data: {json.dumps({'type': 'response.function_call_arguments.done', 'item_id': item_id, 'output_index': output_index, 'arguments': item['arguments']})}\n\n"
                    yield f"data: {json.dumps({'type': 'response.output_item.done', 'output_index': output_index, 'item': item})}\n\n"
            conversation_reuse.complete_turn(
                turn,
                assistant.history_text,
                resolved_conversation_id=transport_state.conversation_id,
                resolved_transport_session_id=transport_state.session_id,
            )
            _emit_debug_log(
                settings,
                "turn.completed",
                endpoint="responses",
                conversation_id=turn.conversation_id,
                routing_mode=turn.routing_mode,
                assistant_preview=_sanitize_debug_value(full_text),
                assistant_length=len(full_text),
                public_model=profile.public_model_id,
                tool_call_count=len(assistant.tool_calls),
            )
            yield f"data: {json.dumps({'type': 'response.completed', 'response': {'id': resp_id, 'object': 'response', 'created_at': created, 'model': profile.public_model_id, 'status': 'completed', 'output': output, 'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}}})}\n\n"
            return

        item_id = f"msg_{uuid.uuid4().hex}"
        yield f"data: {json.dumps({'type': 'response.created', 'response': {'id': resp_id, 'object': 'response', 'created_at': created, 'model': profile.public_model_id, 'status': 'in_progress', 'output': []}})}\n\n"
        yield f"data: {json.dumps({'type': 'response.output_item.added', 'output_index': 0, 'item': {'id': item_id, 'type': 'message', 'role': 'assistant', 'content': []}})}\n\n"
        yield f"data: {json.dumps({'type': 'response.content_part.added', 'item_id': item_id, 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': ''}})}\n\n"
        full_text = ""
        async for delta in client.chat_stream(
            turn.prompt,
            turn.additional_context,
            turn.attachments,
            conversation_id=turn.conversation_id,
            transport_session_id=turn.transport_session_id,
            is_start_of_session=turn.is_start_of_session,
            transport_state=transport_state,
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
    conversation_reuse.complete_turn(
        turn,
        full_text,
        resolved_conversation_id=transport_state.conversation_id,
        resolved_transport_session_id=transport_state.session_id,
    )
    _emit_debug_log(
        settings,
        "turn.completed",
        endpoint="responses",
        conversation_id=turn.conversation_id,
        routing_mode=turn.routing_mode,
        assistant_preview=_sanitize_debug_value(full_text),
        assistant_length=len(full_text),
        public_model=profile.public_model_id,
        tool_call_count=0,
    )
    yield f"data: {json.dumps({'type': 'response.completed', 'response': {'id': resp_id, 'object': 'response', 'created_at': created, 'model': profile.public_model_id, 'status': 'completed', 'output': [{'id': item_id, 'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': full_text}]}], 'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}}})}\n\n"


async def _anthropic_stream(
    model_alias: str,
    settings: Settings,
    client: SubstrateCopilotClient,
    conversation_reuse: ConversationReuseService,
    turn: PreparedConversationTurn,
) -> AsyncIterator[str]:
    msg_id = f"msg_{uuid.uuid4().hex}"
    transport_state = ConversationTransportState()

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
            transport_session_id=turn.transport_session_id,
            is_start_of_session=turn.is_start_of_session,
            transport_state=transport_state,
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
    conversation_reuse.complete_turn(
        turn,
        full_text,
        resolved_conversation_id=transport_state.conversation_id,
        resolved_transport_session_id=transport_state.session_id,
    )
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
