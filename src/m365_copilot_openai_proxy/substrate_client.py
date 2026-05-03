from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from urllib.parse import quote

import httpx
import websockets

from .models import TranslatedImage, UploadedImage
from .token_store import decode_jwt_payload

SIGNALR_SEP = "\x1e"
_WS_BASE = "wss://substrate.office.com/m365Copilot/Chathub"
_UPLOAD_URL = "https://substrate.office.com/m365Copilot/UploadFile"

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
    def __init__(self, access_token: str, time_zone: str = "Asia/Tokyo"):
        self._token = access_token
        self._time_zone = time_zone
        try:
            claims = decode_jwt_payload(access_token)
        except Exception as exc:
            raise SubstrateCopilotError(f"Cannot decode access token: {exc}") from exc
        if time.time() > claims.get("exp", 0):
            raise SubstrateCopilotError(
                "Access token expired. Refresh the shared token file with "
                "`copilot-openai-proxy refresh-token` or restart the Playwright refresh daemon."
            )
        self._oid: str = claims["oid"]
        self._tid: str = claims["tid"]

    def _ws_url(self, conv_id: str, session_id: str, req_id: str) -> str:
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
        uploaded_images: list[UploadedImage],
    ) -> str:
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
                "threadLevelGptId": {},
                "traceId": req_id,
                "isStartOfSession": True,
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
                        {
                            "id": image.doc_id,
                            "messageAnnotationMetadata": {
                                "@type": "File",
                                "annotationType": "File",
                                "fileType": image.file_type,
                                "fileName": image.file_name,
                            },
                            "messageAnnotationType": "ImageFile",
                        }
                        for image in uploaded_images
                    ],
                    "experienceType": "Default",
                    "adaptiveCards": [],
                    "clientPreferences": {},
                },
                "plugins": [{"Id": "BingWebSearch", "Source": "BuiltIn"}],
                "isSbsSupported": True,
                "tone": "Magic",
                "renderReferencesBehindEOS": True,
                "disconnectBehavior": "continue",
            }],
            "invocationId": "0",
            "target": "chat",
            "type": 4,
        }
        return json.dumps(payload, ensure_ascii=False) + SIGNALR_SEP

    async def _upload_image(
        self,
        image: TranslatedImage,
        *,
        conversation_id: str,
    ) -> UploadedImage:
        form_fields: list[tuple[str, tuple[None, str]]] = [
            ("scenario", (None, "UploadImage")),
            ("conversationId", (None, conversation_id)),
            ("FileBase64", (None, image.data_url)),
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

        return UploadedImage(
            doc_id=doc_id,
            file_name=image.filename,
            file_type=uploaded_file_type or image.file_extension,
            uploaded_file_name=uploaded_file_name if isinstance(uploaded_file_name, str) else None,
        )

    async def _upload_images(self, images: list[TranslatedImage]) -> list[UploadedImage]:
        if not images:
            return []
        conversation_id = str(uuid.uuid4())
        uploaded_images: list[UploadedImage] = []
        for image in images:
            uploaded_images.append(
                await self._upload_image(image, conversation_id=conversation_id)
            )
        return uploaded_images

    async def chat_stream(
        self,
        prompt: str,
        additional_context: list[str],
        images: list[TranslatedImage] | None = None,
    ) -> AsyncIterator[str]:
        text = _combine_text(prompt, additional_context)
        conv_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())
        req_id = str(uuid.uuid4())
        url = self._ws_url(conv_id, session_id, req_id)
        uploaded_images = await self._upload_images(images or [])
        try:
            async with websockets.connect(
                url,
                additional_headers={
                    "Origin": "https://m365.cloud.microsoft",
                },
            ) as ws:
                await ws.send(json.dumps({"protocol": "json", "version": 1}) + SIGNALR_SEP)
                await ws.recv()
                await ws.send(self._chat_invoke(text, conv_id, session_id, req_id, uploaded_images))
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
        images: list[TranslatedImage] | None = None,
    ) -> str:
        chunks: list[str] = []
        async for chunk in self.chat_stream(prompt, additional_context, images):
            chunks.append(chunk)
        return "".join(chunks)


def _combine_text(prompt: str, context: list[str]) -> str:
    if not context:
        return prompt
    if not prompt:
        return "\n\n".join(context)
    return "\n\n".join(context) + "\n\n---\n\n" + prompt
