from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
import base64
from urllib.parse import quote
from uuid import UUID

import httpx
import websockets

from .copilot_models import CopilotModelTransport
from .models import AuthSessionSnapshot, TranslatedAttachment, UploadedAttachment
from .token_store import build_enterprise_auth_session

SIGNALR_SEP = "\x1e"
_WS_BASE = "wss://substrate.office.com/m365Copilot/Chathub"
_UPLOAD_URL = "https://substrate.office.com/m365Copilot/UploadFile"
_PERSONAL_UPLOAD_CREATE_URL = "https://graph.microsoft.com/v1.0/me/drive/special/copilotuploads:/{filename}:/createUploadSession"
_PERSONAL_UNFURL_URL = "https://substrate.office.com/searchservice/api/v1/unfurl?domain=File"
_GRAPH_UPLOAD_CHUNK_SIZE = 1310720
_VARIANTS = (
    "EnableMcpServerWidgets,feature.EnableMcpServerWidgets,feature.EnableLuForChatCIQ,"
    "feature.enableChatCIQPlugin,EnableRequestPlugins,feature.EnableSensitivityLabels,"
    "EnableUnsupportedUrlDetector,feature.IsCustomEngineCopilotEnabled,feature.bizchatfluxv3,"
    "feature.enablechatpages,feature.enableCodeCanvas,feature.turnOnWorkTabRecommendation,"
    "feature.turnOnDARecommendation,feature.IsStreamingModeInChatRequestEnabled,"
    "IncludeSourceAttributionsConcise,SkipPublishEmptyMessage,"
    "feature.EnableDeduplicatingSourceAttributions,Enable3PActionProgressMessages,"
    "feature.enableClientWebRtc,feature.EnableMeetingRecapOfSeriesMeetingWithCiq,"
    "feature.EnableReferencesListCompleteSignal,feature.StorageMessageSplitDisabled,"
    "feature.EnableCuaTakeControlApi,SingletonEnvOn,feature.cwcallowedos,"
    "feature.EnableMergingPureDeltas,feature.disabledisallowedmsgs,"
    "feature.enableCitationsForSynthesisData,feature.EnableConversationShareApis,"
    "feature.enableGenerateGraphicArtOptionsSet,cdximagen,"
    "feature.EnableUpdatedUXForConfirmationDialog,"
    "feature.EnableContentApiandDocTypeHtmlInRichAnswers,"
    "cdxgrounding_api_v2_rich_web_answers_reference_bottom_force,"
    "cdxenablerenderforisocomp,feature.EnableClientFileURLSupportForOfficeWebPaidCopilot,"
    "feature.EnableDesignEditorImageGrounding,feature.EnableDesignerEditor,"
    "feature.EnableBase64DataInMessageAnnotations,feature.EnablePersonalization,"
    "feature.EnableSkipRehydrationForSpeCIdImages,feature.EnableSkipEmittingMessageOnFlush,"
    "feature.EnableRemoveEmptySourceAttributions,feature.EnableRemoveStreamingMode,"
    "feature.OfficeWebToHelix,feature.OfficeDesktopToHelix,feature.M365TeamsHubToHelix,"
    "feature.OwaHubToHelix,feature.MonarchHubToHelix,feature.Win32OutlookHubToHelix,"
    "feature.MacOutlookHubToHelix,Agt_bizchat_enableGpt5ForHelix,"
    "agt_bizchat_enableRichResponses"
)

_OPTIONS_SETS = [
    "search_result_progress_messages_with_search_queries",
    "cwc_flux_image",
    "cwc_code_interpreter",
    "cwc_code_interpreter_amsfix",
    "cwcfluxgptv",
    "flux_v3_gptv_enable_upload_multi_image_in_turn_wo_ch",
    "cwc_code_interpreter_citation_fix",
    "code_interpreter_interactive_charts",
    "cwc_code_interpreter_interactive_charts_inline_image",
    "code_interpreter_matplotlib_patching",
    "cwc_fileupload_odb",
    "update_memory_plugin",
    "add_custom_instructions",
    "cwc_flux_v3",
    "flux_v3_progress_messages",
    "enable_batch_token_processing",
    "enable_gg_gpt",
    "flux_v3_image_gen_enable_dimensions",
    "flux_v3_image_gen_enable_icon_dimensions",
    "flux_v3_image_gen_enable_system_text_with_params",
    "flux_v3_image_gen_enable_designer_dimensions_meta_prompting_in_system_prompts",
    "flux_v3_image_gen_enable_story",
    "gptvnorm2048",
    "rich_responses",
]

_UPLOAD_OPTIONS_SETS = [
    "cwcgptvsan",
    "flux_v3_gptv_enable_upload_multi_image_in_turn_wo_ch",
    "gptvnorm2048",
]

_ALLOWED_MESSAGE_TYPES = [
    "Chat", "Suggestion", "InternalSearchQuery", "Disengaged",
    "InternalLoaderMessage", "Progress", "GeneratedCode", "RenderCardRequest",
    "AdsQuery", "SemanticSerp", "GenerateContentQuery", "GenerateGraphicArt",
    "SearchQuery", "ConfirmationCard", "AuthError", "DeveloperLogs",
    "TriggerPlugin", "HintInvocation", "MemoryUpdate", "EndOfRequest",
    "TriggerConfirmation", "ResumeInvokeAction", "ResumeUserInputRequest",
    "TriggerUserInputRequest", "EscapeHatch", "TriggerPluginAuth",
    "ResumePluginAuth", "SideBySide", "ReferencesListComplete",
    "ComputerToolsRoomInfo", "SwitchRespondingEndpoint",
]


class SubstrateCopilotError(RuntimeError):
    pass

class SubstrateCopilotClient:
    def __init__(
        self,
        auth_session: AuthSessionSnapshot | str,
        time_zone: str = "Asia/Tokyo",
        *,
        model_transport: CopilotModelTransport | None = None,
    ):
        self._time_zone = time_zone
        self._model_transport = model_transport
        if isinstance(auth_session, str):
            try:
                auth_session = build_enterprise_auth_session(auth_session)
            except Exception as exc:
                raise SubstrateCopilotError(f"Cannot decode access token: {exc}") from exc
        self._auth_session = auth_session
        self._token = auth_session.access_token
        self._graph_token = auth_session.graph_access_token or auth_session.access_token
        self._search_token = auth_session.search_access_token or auth_session.access_token
        if auth_session.expires_at is not None and time.time() > auth_session.expires_at:
            raise SubstrateCopilotError(
                "Access token expired. Refresh the shared token file with "
                "`copilot-openai-proxy refresh-token` or restart the Playwright refresh daemon."
            )
        self._oid = auth_session.oid
        self._tid = auth_session.tid
        self._websocket_url = auth_session.websocket_url

    def _ws_url(self, conv_id: str, session_id: str, req_id: str) -> str:
        if self._auth_session.account_mode == "personal":
            if not self._websocket_url:
                raise SubstrateCopilotError(
                    "Personal auth session is missing a captured Copilot WebSocket URL. "
                    "Re-run `copilot-openai-proxy login` or `copilot-openai-proxy refresh-token`."
                )
            return self._websocket_url
        token = quote(self._token, safe="")
        return (
            f"{_WS_BASE}/{self._oid}@{self._tid}"
            f"?ClientRequestId={req_id}"
            f"&X-SessionId={session_id}"
            f"&ConversationId={conv_id}"
            f"&access_token={token}"
            f"&variants={_VARIANTS}"
            f"&source=officeweb&product=Office&agentHost=Bizchat.FullScreen"
            f"&licenseType=Starter&agent=web&scenario=OfficeWebIncludedCopilot"
        )

    def _chat_invoke(
        self,
        text: str,
        conv_id: str,
        session_id: str,
        req_id: str,
        uploaded_attachments: list[UploadedAttachment],
        *,
        is_start_of_session: bool,
    ) -> str:
        thread_level_gpt_id = {}
        tone = None
        if self._model_transport is not None:
            thread_level_gpt_id = dict(self._model_transport.thread_level_gpt_id)
            tone = self._model_transport.tone
        payload = {
            "arguments": [{
                "source": "officeweb",
                "clientCorrelationId": req_id,
                "sessionId": session_id,
                "optionsSets": _OPTIONS_SETS,
                "streamingMode": "ConciseWithPadding",
                "spokenTextMode": "None",
                "options": {},
                "extraExtensionParameters": {},
                "allowedMessageTypes": _ALLOWED_MESSAGE_TYPES,
                "sliceIds": [],
                "threadLevelGptId": thread_level_gpt_id,
                "traceId": req_id,
                "isStartOfSession": is_start_of_session,
                "clientInfo": {
                    "clientPlatform": "mcmcopilot-web",
                    "clientAppName": "Office",
                    "clientEntrypoint": "mcmcopilot-officeweb",
                    "clientSessionId": session_id,
                    "ProductCategory": "Chat",
                    "clientAppType": "Web",
                    "productEntryPoint": "ChatPanel",
                    "deviceOS": "Windows",
                    "deviceType": "Desktop",
                },
                "message": {
                    "author": "user",
                    "inputMethod": "Keyboard",
                    "text": text,
                    "entityAnnotationTypes": ["People", "File", "Event", "Email", "TeamsMessage"],
                    "requestId": req_id,
                    "locationInfo": {"timeZoneOffset": 9, "timeZone": self._time_zone},
                    "locale": "en-us",
                    "messageType": "Chat",
                    "messageAnnotations": [
                        _build_message_annotation(attachment)
                        for attachment in uploaded_attachments
                    ],
                    "experienceType": "Default",
                    "adaptiveCards": [],
                    "clientPreferences": {},
                },
                "plugins": [{"Id": "BingWebSearch", "Source": "BuiltIn"}],
                "isSbsSupported": True,
                "renderReferencesBehindEOS": True,
                "disconnectBehavior": "continue",
            }],
            "invocationId": "0",
            "target": "chat",
            "type": 4,
        }
        if tone:
            payload["arguments"][0]["tone"] = tone
        return json.dumps(payload, ensure_ascii=False) + SIGNALR_SEP

    async def _upload_enterprise_attachment(
        self,
        attachment: TranslatedAttachment,
        *,
        conversation_id: str,
    ) -> UploadedAttachment:
        if attachment.kind != "image":
            raise SubstrateCopilotError(
                "Enterprise file attachments are not supported yet. Use a personal account mode session for file uploads."
            )
        form_fields: list[tuple[str, tuple[None, str]]] = [
            ("scenario", (None, "UploadImage")),
            ("conversationId", (None, conversation_id)),
            ("FileBase64", (None, attachment.data_url)),
        ]
        form_fields.extend(("optionsSets", (None, value)) for value in _UPLOAD_OPTIONS_SETS)
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "*/*",
            "Origin": "https://m365.cloud.microsoft",
            "Referer": "https://m365.cloud.microsoft/",
            "x-anchormailbox": f"Oid:{self._oid}@{self._tid}",
            "x-scenario": "OfficeWebIncludedCopilot",
            "x-variants": "feature.EnableImageSupportInUploadFile",
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(_UPLOAD_URL, files=form_fields, headers=headers)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise SubstrateCopilotError(f"Image upload failed: {exc}") from exc

        doc_id = payload.get("docId")
        if not isinstance(doc_id, str) or not doc_id:
            raise SubstrateCopilotError("Image upload failed: missing docId in UploadFile response.")

        uploaded_file_name = payload.get("fileName")
        uploaded_file_type = payload.get("fileType")
        if uploaded_file_type is not None and isinstance(uploaded_file_type, str):
            uploaded_file_type = uploaded_file_type.removeprefix(".")

        return UploadedAttachment(
            kind=attachment.kind,
            doc_id=doc_id,
            file_name=attachment.filename,
            file_type=uploaded_file_type or attachment.file_extension,
            uploaded_file_name=uploaded_file_name if isinstance(uploaded_file_name, str) else None,
        )

    async def _upload_graph_file_attachment(
        self,
        attachment: TranslatedAttachment,
        *,
        conversation_id: str,
    ) -> UploadedAttachment:
        graph_headers = {
            "Authorization": f"Bearer {self._graph_token}",
            "Accept": "*/*",
            "Origin": "https://m365.cloud.microsoft",
            "Referer": "https://m365.cloud.microsoft/",
        }
        substrate_headers = {
            "Authorization": f"Bearer {self._search_token}",
            "Accept": "*/*",
            "Origin": "https://m365.cloud.microsoft",
            "Referer": "https://m365.cloud.microsoft/",
        }
        create_url = _PERSONAL_UPLOAD_CREATE_URL.format(filename=quote(attachment.filename, safe=""))
        create_body = {
            "item": {
                "@microsoft.graph.conflictBehavior": "replace",
                "name": attachment.filename,
            }
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                create_response = await client.post(
                    create_url,
                    headers={**graph_headers, "Content-Type": "application/json"},
                    json=create_body,
                )
                create_response.raise_for_status()
                create_payload = create_response.json()
                upload_url = create_payload.get("uploadUrl")
                if not isinstance(upload_url, str) or not upload_url:
                    raise SubstrateCopilotError(
                        "File upload failed: missing uploadUrl in createUploadSession response."
                    )
                size = len(attachment.content)
                upload_payload = None
                for start in range(0, size, _GRAPH_UPLOAD_CHUNK_SIZE):
                    end = min(start + _GRAPH_UPLOAD_CHUNK_SIZE, size) - 1
                    upload_response = await client.put(
                        upload_url,
                        headers={
                            "Content-Type": "application/octet-stream",
                            "Content-Range": f"bytes {start}-{end}/{size}",
                        },
                        content=attachment.content[start : end + 1],
                    )
                    upload_response.raise_for_status()
                    upload_payload = upload_response.json()
                if not isinstance(upload_payload, dict):
                    raise SubstrateCopilotError(
                        "File upload failed: missing final upload metadata."
                    )
                drive_item_id = upload_payload.get("id")
                parent_reference = upload_payload.get("parentReference") or {}
                drive_id = parent_reference.get("driveId")
                drive_type = parent_reference.get("driveType")
                if not isinstance(drive_item_id, str) or not drive_item_id:
                    raise SubstrateCopilotError(
                        "File upload failed: missing drive item id in upload response."
                    )
                if not isinstance(drive_id, str) or not drive_id:
                    raise SubstrateCopilotError(
                        "File upload failed: missing drive id in upload response."
                    )
                doc_id = _build_graph_file_doc_id(
                    drive_type=drive_type if isinstance(drive_type, str) else "",
                    drive_id=drive_id,
                    drive_item_id=drive_item_id,
                )
                logical_id = str(uuid.uuid4())
                client_request_id = str(uuid.uuid4())
                client_session_id = str(uuid.uuid4())
                unfurl_body = {
                    "EntityRequests": [
                        {
                            "QueryAnnotations": [
                                {
                                    "Id": doc_id,
                                    "Type": "LocalFile",
                                    "Text": attachment.filename,
                                }
                            ],
                            "PreferredResultSourceFormat": "EntityData",
                            "SupportedResultSourceFormats": ["EntityData"],
                        }
                    ],
                    "LogicalId": logical_id,
                    "Cvid": conversation_id,
                    "Scenario": {
                        "Name": "Harmony.Web.Copilot_Drawer",
                        "Dimensions": [
                            {
                                "DimensionName": "ScenarioDescription",
                                "DimensionValue": _file_upload_scenario_description(
                                    self._auth_session.account_mode
                                ),
                            },
                            {
                                "DimensionName": "ScenarioType",
                                "DimensionValue": "PO",
                            },
                        ],
                    },
                    "CacheMode": "FireForget",
                }
                unfurl_response = await client.post(
                    _PERSONAL_UNFURL_URL,
                    headers={
                        **substrate_headers,
                        "Content-Type": "application/json",
                        "x-anchormailbox": f"Oid:{self._oid}@{self._tid}",
                        "x-routingparameter-sessionkey": f"Oid:{self._oid}@{self._tid}",
                        "client-request-id": client_request_id,
                        "client-session-id": client_session_id,
                        "x-client-language": "en-us",
                        "x-client-localtime": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                    },
                    json=unfurl_body,
                )
                unfurl_response.raise_for_status()
        except SubstrateCopilotError:
            raise
        except Exception as exc:
            raise SubstrateCopilotError(f"File upload failed: {exc}") from exc

        uploaded_file_name = upload_payload.get("name")
        uploaded_file_type = ((upload_payload.get("file") or {}).get("fileExtension"))
        uploaded_file_url = upload_payload.get("webUrl")
        if uploaded_file_type is not None and isinstance(uploaded_file_type, str):
            uploaded_file_type = uploaded_file_type.removeprefix(".")
        if not isinstance(uploaded_file_url, str):
            uploaded_file_url = None
        return UploadedAttachment(
            kind=attachment.kind,
            doc_id=doc_id,
            file_name=attachment.filename,
            file_type=uploaded_file_type or attachment.file_extension,
            uploaded_file_name=uploaded_file_name if isinstance(uploaded_file_name, str) else None,
            logical_id=logical_id,
            annotation_type="LocalFile",
            annotation_text=attachment.filename,
            annotation_url=_graph_file_annotation_url(
                drive_type=drive_type if isinstance(drive_type, str) else "",
                drive_id=drive_id,
                drive_item_id=drive_item_id,
                uploaded_file_url=uploaded_file_url,
            ),
        )

    async def _upload_attachment(
        self,
        attachment: TranslatedAttachment,
        *,
        upload_conversation_id: str,
        chat_conversation_id: str,
    ) -> UploadedAttachment:
        if attachment.kind == "file":
            return await self._upload_graph_file_attachment(
                attachment,
                conversation_id=chat_conversation_id,
            )
        if self._auth_session.account_mode == "personal":
            attachment = _convert_image_attachment_to_pdf(attachment)
            return await self._upload_graph_file_attachment(
                attachment,
                conversation_id=chat_conversation_id,
            )
        return await self._upload_enterprise_attachment(
            attachment,
            conversation_id=upload_conversation_id,
        )

    async def _upload_image(
        self,
        image: TranslatedAttachment,
        *,
        conversation_id: str,
    ) -> UploadedAttachment:
        return await self._upload_attachment(
            image,
            upload_conversation_id=conversation_id,
            chat_conversation_id=conversation_id,
        )

    async def _upload_attachments(
        self,
        attachments: list[TranslatedAttachment],
        *,
        chat_conversation_id: str,
    ) -> list[UploadedAttachment]:
        if not attachments:
            return []
        upload_conversation_id = str(uuid.uuid4())
        uploaded_attachments: list[UploadedAttachment] = []
        for attachment in attachments:
            uploaded_attachments.append(
                await self._upload_attachment(
                    attachment,
                    upload_conversation_id=upload_conversation_id,
                    chat_conversation_id=chat_conversation_id,
                )
            )
        return uploaded_attachments

    async def _upload_images(
        self,
        images: list[TranslatedAttachment],
    ) -> list[UploadedAttachment]:
        return await self._upload_attachments(images, chat_conversation_id=str(uuid.uuid4()))

    async def chat_stream(
        self,
        prompt: str,
        additional_context: list[str],
        attachments: list[TranslatedAttachment] | None = None,
        *,
        conversation_id: str,
        is_start_of_session: bool,
    ) -> AsyncIterator[str]:
        text = _combine_text(prompt, additional_context)
        conv_id = conversation_id
        session_id = str(uuid.uuid4())
        req_id = str(uuid.uuid4())
        url = self._ws_url(conv_id, session_id, req_id)
        uploaded_attachments = await self._upload_attachments(
            attachments or [],
            chat_conversation_id=conv_id,
        )
        try:
            async with websockets.connect(
                url,
                additional_headers={
                    "Origin": "https://m365.cloud.microsoft",
                },
            ) as ws:
                await ws.send(json.dumps({"protocol": "json", "version": 1}) + SIGNALR_SEP)
                await ws.recv()
                await ws.send(
                    self._chat_invoke(
                        text,
                        conv_id,
                        session_id,
                        req_id,
                        uploaded_attachments,
                        is_start_of_session=is_start_of_session,
                    )
                )
                fallback_text = ""
                yielded_any = False
                async for raw in ws:
                    for part in raw.split(SIGNALR_SEP):
                        part = part.strip()
                        if not part:
                            continue
                        try:
                            msg = json.loads(part)
                        except json.JSONDecodeError:
                            continue
                        t = msg.get("type")
                        if t == 6:
                            continue
                        if t == 1 and msg.get("target") == "update":
                            args = (msg.get("arguments") or [{}])[0]
                            delta = args.get("writeAtCursor")
                            if delta:
                                if not yielded_any and fallback_text:
                                    yield fallback_text
                                yielded_any = True
                                yield delta
                            msgs = args.get("messages")
                            if msgs:
                                entries = msgs if isinstance(msgs, list) else [msgs]
                                for entry in reversed(entries):
                                    if entry.get("author") != "user":
                                        fallback_text = entry.get("text", "")
                                        break
                        if t == 2:
                            item_msgs = (msg.get("item") or {}).get("messages") or []
                            for entry in reversed(item_msgs):
                                if entry.get("author") != "user":
                                    fallback_text = entry.get("text", "")
                                    break
                        if t == 3:
                            if not yielded_any and fallback_text:
                                yield fallback_text
                            return
        except SubstrateCopilotError:
            raise
        except Exception as exc:
            raise SubstrateCopilotError(str(exc)) from exc

    async def chat(
        self,
        prompt: str,
        additional_context: list[str],
        attachments: list[TranslatedAttachment] | None = None,
        *,
        conversation_id: str,
        is_start_of_session: bool,
    ) -> str:
        chunks: list[str] = []
        async for chunk in self.chat_stream(
            prompt,
            additional_context,
            attachments,
            conversation_id=conversation_id,
            is_start_of_session=is_start_of_session,
        ):
            chunks.append(chunk)
        return "".join(chunks)


def _combine_text(prompt: str, context: list[str]) -> str:
    if not context:
        return prompt
    if not prompt:
        return "\n\n".join(context)
    return "\n\n".join(context) + "\n\n---\n\n" + prompt


def _message_annotation_type(attachment: UploadedAttachment) -> str:
    return "ImageFile" if attachment.kind == "image" else "File"


def _build_message_annotation_metadata(attachment: UploadedAttachment) -> dict[str, str]:
    metadata = {
        "@type": "File",
        "annotationType": "File",
        "fileType": attachment.file_type,
        "fileName": attachment.file_name,
    }
    if attachment.logical_id:
        metadata["logicalId"] = attachment.logical_id
    if attachment.entity_id:
        metadata["entityId"] = attachment.entity_id
    return metadata


def _build_message_annotation(attachment: UploadedAttachment) -> dict[str, object]:
    if attachment.annotation_type:
        annotation: dict[str, object] = {
            "id": attachment.doc_id,
            "messageAnnotationType": attachment.annotation_type,
        }
        if attachment.annotation_text:
            annotation["text"] = attachment.annotation_text
        if attachment.annotation_url:
            annotation["url"] = attachment.annotation_url
        return annotation
    return {
        "id": attachment.doc_id,
        "messageAnnotationMetadata": _build_message_annotation_metadata(attachment),
        "messageAnnotationType": _message_annotation_type(attachment),
    }


def _file_upload_scenario_description(account_mode: str) -> str:
    if account_mode == "personal":
        return "OfficeWebPremiumConsumerCopilot.prefetch.getdocumentsummary.fileupload"
    return "OfficeWebIncludedCopilot.prefetch.getdocumentsummary.fileupload"


def _build_graph_file_doc_id(*, drive_type: str, drive_id: str, drive_item_id: str) -> str:
    if drive_type == "business":
        site_id, web_id, list_id = _decode_business_drive_id(drive_id)
        joined_drive_tuple = f"{site_id},{web_id},{list_id}"
        encoded_drive_tuple = base64.urlsafe_b64encode(joined_drive_tuple.encode("utf-8")).decode("ascii").rstrip("=")
        return f"SPO_{encoded_drive_tuple}_{drive_item_id}"
    return f"SPO_{drive_id}_{drive_item_id}"


def _graph_file_annotation_url(
    *,
    drive_type: str,
    drive_id: str,
    drive_item_id: str,
    uploaded_file_url: str | None,
) -> str | None:
    if drive_type == "business":
        return uploaded_file_url
    return f"https://onedrive.live.com?cid={drive_id}&id={drive_item_id}"


def _convert_image_attachment_to_pdf(attachment: TranslatedAttachment) -> TranslatedAttachment:
    if attachment.kind != "image":
        return attachment
    try:
        import img2pdf
    except ImportError as exc:
        raise SubstrateCopilotError(
            "Personal image uploads require the `img2pdf` package. Reinstall the project dependencies."
        ) from exc

    try:
        pdf_content = img2pdf.convert(attachment.content)
    except Exception as exc:
        raise SubstrateCopilotError(f"Personal image-to-PDF conversion failed: {exc}") from exc

    stem, _separator, _suffix = attachment.filename.rpartition(".")
    pdf_filename = f"{stem or attachment.filename}.pdf"
    pdf_data_url = "data:application/pdf;base64," + base64.b64encode(pdf_content).decode("ascii")
    return TranslatedAttachment(
        kind="image",
        filename=pdf_filename,
        mime_type="application/pdf",
        file_extension="pdf",
        data_url=pdf_data_url,
        content=pdf_content,
    )


def _decode_business_drive_id(drive_id: str) -> tuple[str, str, str]:
    encoded = drive_id[2:] if drive_id.startswith("b!") else drive_id
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except Exception as exc:
        raise SubstrateCopilotError(f"File upload failed: cannot decode business drive id: {exc}") from exc
    if len(raw) != 48:
        raise SubstrateCopilotError(
            f"File upload failed: unexpected business drive id payload length {len(raw)}."
        )
    return tuple(str(UUID(bytes_le=raw[index : index + 16])) for index in range(0, 48, 16))  # type: ignore[return-value]
