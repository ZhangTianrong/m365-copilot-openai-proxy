from __future__ import annotations

import asyncio
import json

import httpx

from m365_copilot_openai_proxy.copilot_models import (
    CopilotModelTransport,
    resolve_copilot_model_transport,
)
from m365_copilot_openai_proxy.models import (
    AuthSessionSnapshot,
    ConversationTransportState,
    TranslatedImage,
    UploadedImage,
)
from m365_copilot_openai_proxy.substrate_client import (
    SIGNALR_SEP,
    SubstrateCopilotClient,
)

_TEST_JWT = (
    "eyJhbGciOiJub25lIn0."
    "eyJvaWQiOiIxMjM0NTY3OC0xMjM0LTEyMzQtMTIzNC0xMjM0NTY3ODkwYWIiLCJ0aWQiOiJhYmNkZWYwMS0yMzQ1LTY3ODktYWJjZC1lZjAxMjM0NTY3ODkiLCJleHAiOjQxMDAwMDAwMDB9."
)


def build_client() -> SubstrateCopilotClient:
    return SubstrateCopilotClient(_TEST_JWT, "America/New_York")


def build_personal_client(*, search_access_token: str | None = None) -> SubstrateCopilotClient:
    return SubstrateCopilotClient(
        AuthSessionSnapshot(
            account_mode="personal",
            access_token="eyJhbGciOiJkaXIifQ.test.encrypted.value.more",
            expires_at=4100000000,
            captured_at=123,
            oid="00000000-0000-0000-853e-527a6bf3c11e",
            tid="84df9e7f-e9f6-40af-b435-aaaaaaaaaaaa",
            websocket_url=(
                "wss://substrate.office.com/m365Copilot/Chathub/"
                "00000000-0000-0000-853e-527a6bf3c11e@84df9e7f-e9f6-40af-b435-aaaaaaaaaaaa"
                "?access_token=eyJhbGciOiJkaXIifQ.test.encrypted.value.more"
                "&XRoutingParameterSessionKey=session-key"
                "&clientrequestid=request-id"
                "&chatsessionid=chat-session-id"
                "&licenseType=Starter&agent=web&scenario=OfficeWebIncludedCopilot"
            ),
            search_access_token=search_access_token,
            search_expires_at=4100000200 if search_access_token else None,
        ),
        "America/New_York",
    )


def build_enterprise_snapshot_client(*, search_access_token: str | None = None) -> SubstrateCopilotClient:
    return SubstrateCopilotClient(
        AuthSessionSnapshot(
            account_mode="enterprise",
            access_token=_TEST_JWT,
            expires_at=4100000000,
            captured_at=123,
            oid="12345678-1234-1234-1234-1234567890ab",
            tid="abcdef01-2345-6789-abcd-ef0123456789",
            graph_access_token="graph-token",
            graph_expires_at=4100000100,
            search_access_token=search_access_token,
            search_expires_at=4100000200 if search_access_token else None,
        ),
        "America/New_York",
    )


def test_chat_invoke_includes_image_annotations() -> None:
    client = build_client()
    uploaded_images = [
        UploadedImage(doc_id="doc-1", file_name="image.png", file_type="png"),
        UploadedImage(doc_id="doc-2", file_name="image-2.jpg", file_type="jpg"),
    ]

    payload = client._chat_invoke(
        "describe",
        "conv-1",
        "session-1",
        "req-1",
        uploaded_images,
        is_start_of_session=True,
    )
    body = json.loads(payload.removesuffix(SIGNALR_SEP))
    message = body["arguments"][0]["message"]

    assert message["text"] == "describe"
    assert message["messageAnnotations"] == [
        {
            "id": "doc-1",
            "messageAnnotationMetadata": {
                "@type": "File",
                "annotationType": "File",
                "fileType": "png",
                "fileName": "image.png",
            },
            "messageAnnotationType": "ImageFile",
        },
        {
            "id": "doc-2",
            "messageAnnotationMetadata": {
                "@type": "File",
                "annotationType": "File",
                "fileType": "jpg",
                "fileName": "image-2.jpg",
            },
            "messageAnnotationType": "ImageFile",
        },
    ]


def test_chat_invoke_includes_file_annotations_for_non_images() -> None:
    client = build_personal_client()
    uploaded_files = [
        UploadedImage(
            kind="file",
            doc_id="SPO_drive_item",
            file_name="receipt.pdf",
            file_type="pdf",
            annotation_type="LocalFile",
            annotation_text="receipt.pdf",
            annotation_url="https://onedrive.live.com?cid=drive&id=item",
        ),
    ]

    payload = client._chat_invoke(
        "summarize this",
        "conv-1",
        "session-1",
        "req-1",
        uploaded_files,
        is_start_of_session=True,
    )
    body = json.loads(payload.removesuffix(SIGNALR_SEP))
    message = body["arguments"][0]["message"]

    assert message["messageAnnotations"] == [
        {
            "id": "SPO_drive_item",
            "text": "receipt.pdf",
            "url": "https://onedrive.live.com?cid=drive&id=item",
            "messageAnnotationType": "LocalFile",
        }
    ]


def test_chat_invoke_includes_selected_copilot_model_tone() -> None:
    client = SubstrateCopilotClient(
        _TEST_JWT,
        "America/New_York",
        model_transport=CopilotModelTransport(
            visible_name="GPT 5.5 Think Deeper",
            tone="Gpt_5_5_Reasoning",
        ),
    )

    payload = client._chat_invoke(
        "describe",
        "conv-1",
        "session-1",
        "req-1",
        [],
        is_start_of_session=True,
    )
    body = json.loads(payload.removesuffix(SIGNALR_SEP))

    assert body["arguments"][0]["threadLevelGptId"] == {}
    assert body["arguments"][0]["tone"] == "Gpt_5_5_Reasoning"


def test_resolve_copilot_model_transport_exact_match_and_fallback() -> None:
    transport, warning = resolve_copilot_model_transport("GPT 5.4 Think Deeper")
    assert transport is not None
    assert transport.visible_name == "GPT 5.4 Think Deeper"
    assert transport.tone == "Gpt_5_4_Reasoning"
    assert transport.thread_level_gpt_id == {}
    assert warning is None

    transport, warning = resolve_copilot_model_transport("gpt 5.4 think deeper")
    assert transport is None
    assert warning is not None
    assert "validated transport mapping yet" in warning


def test_resolve_copilot_model_transport_auto() -> None:
    transport, warning = resolve_copilot_model_transport("Auto")
    assert transport is not None
    assert transport.visible_name == "Auto"
    assert transport.tone == "Magic"
    assert warning is None


def test_chat_invoke_omits_tone_without_selected_model() -> None:
    client = build_client()

    payload = client._chat_invoke(
        "describe",
        "conv-1",
        "session-1",
        "req-1",
        [],
        is_start_of_session=True,
    )
    body = json.loads(payload.removesuffix(SIGNALR_SEP))
    assert "tone" not in body["arguments"][0]


def test_personal_ws_url_uses_dynamic_turn_parameters() -> None:
    client = build_personal_client()

    assert client._ws_url(
        "conv-1",
        "session-1",
        "req-1",
        is_start_of_session=False,
    ) == (
        "wss://substrate.office.com/m365Copilot/Chathub/"
        "00000000-0000-0000-853e-527a6bf3c11e@84df9e7f-e9f6-40af-b435-aaaaaaaaaaaa"
        "?licenseType=Starter&agent=web&scenario=OfficeWebIncludedCopilot"
        "&chatsessionid=req-1"
        "&XRoutingParameterSessionKey=req-1"
        "&clientrequestid=req-1"
        "&X-SessionId=session-1"
        "&access_token=eyJhbGciOiJkaXIifQ.test.encrypted.value.more"
        "&ConversationId=conv-1"
    )


def test_personal_ws_url_omits_conversation_id_on_first_turn() -> None:
    client = build_personal_client()

    assert client._ws_url(
        "conv-1",
        "session-1",
        "req-1",
        is_start_of_session=True,
    ) == (
        "wss://substrate.office.com/m365Copilot/Chathub/"
        "00000000-0000-0000-853e-527a6bf3c11e@84df9e7f-e9f6-40af-b435-aaaaaaaaaaaa"
        "?licenseType=Starter&agent=web&scenario=OfficeWebIncludedCopilot"
        "&chatsessionid=req-1"
        "&XRoutingParameterSessionKey=req-1"
        "&clientrequestid=req-1"
        "&X-SessionId=session-1"
        "&access_token=eyJhbGciOiJkaXIifQ.test.encrypted.value.more"
    )


def test_personal_ws_url_keeps_websocket_query_token_when_snapshot_token_differs() -> None:
    client = SubstrateCopilotClient(
        AuthSessionSnapshot(
            account_mode="personal",
            access_token="substrate-rest-token-from-storage",
            expires_at=4100000000,
            captured_at=123,
            oid="00000000-0000-0000-853e-527a6bf3c11e",
            tid="84df9e7f-e9f6-40af-b435-aaaaaaaaaaaa",
            websocket_url=(
                "wss://substrate.office.com/m365Copilot/Chathub/"
                "00000000-0000-0000-853e-527a6bf3c11e@84df9e7f-e9f6-40af-b435-aaaaaaaaaaaa"
                "?access_token=ws-query-token"
                "&licenseType=Premium&agent=web&scenario=OfficeWebPremiumConsumerCopilot"
            ),
        ),
        "America/New_York",
    )

    url = client._ws_url(
        "conv-1",
        "session-1",
        "req-1",
        is_start_of_session=True,
    )

    assert "access_token=ws-query-token" in url
    assert "substrate-rest-token-from-storage" not in url


def test_personal_upload_uses_turn_scoped_unfurl_cvid(monkeypatch) -> None:
    client = build_personal_client(search_access_token="search-token")
    attachment = TranslatedImage(
        kind="file",
        filename="image.pdf",
        mime_type="application/pdf",
        file_extension="pdf",
        data_url="data:application/pdf;base64,aGVsbG8=",
        content=b"hello",
    )
    captured: dict[str, object] = {"post_calls": []}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, files=None, headers=None, json=None):
            captured["post_calls"].append({"url": url, "json": json, "headers": headers})
            request = httpx.Request("POST", url)
            if "createUploadSession" in url:
                return httpx.Response(
                    200,
                    request=request,
                    json={"uploadUrl": "https://upload.example/personal-session"},
                )
            return httpx.Response(200, request=request, json={"ApiVersion": "1.0"})

        async def put(self, url, headers=None, content=None):
            request = httpx.Request("PUT", url)
            return httpx.Response(
                201,
                request=request,
                    json={
                        "id": "item-1",
                        "name": "image.pdf",
                        "file": {"fileExtension": ".pdf"},
                    "parentReference": {
                        "driveType": "personal",
                        "driveId": "drive-1",
                    },
                },
            )

    monkeypatch.setattr("m365_copilot_openai_proxy.substrate_client.httpx.AsyncClient", FakeAsyncClient)

    asyncio.run(
        client._upload_attachment(
            attachment,
            upload_conversation_id="upload-conv",
            chat_conversation_id="chat-conv",
        )
    )

    unfurl_call = captured["post_calls"][1]
    assert unfurl_call["json"]["Cvid"] == "upload-conv"
    assert unfurl_call["headers"]["Authorization"] == "Bearer search-token"


def test_personal_chat_stream_extracts_remote_conversation_id(monkeypatch) -> None:
    client = build_personal_client()
    captured_url: dict[str, str] = {}

    class FakeWebSocket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def send(self, data):
            return None

        async def recv(self):
            return "{}" + SIGNALR_SEP

        def __aiter__(self):
            async def generator():
                yield json.dumps(
                    {
                        "type": 2,
                        "item": {
                            "conversationId": "remote-conv-1",
                            "messages": [{"author": "assistant", "text": "hello"}],
                        },
                    }
                ) + SIGNALR_SEP
                yield json.dumps({"type": 3}) + SIGNALR_SEP

            return generator()

    def fake_connect(url, additional_headers=None):
        captured_url["url"] = url
        return FakeWebSocket()

    monkeypatch.setattr("m365_copilot_openai_proxy.substrate_client.websockets.connect", fake_connect)
    async def fake_upload_attachments(attachments, *, chat_conversation_id):
        return []

    monkeypatch.setattr(client, "_upload_attachments", fake_upload_attachments)

    transport_state = ConversationTransportState()
    text = asyncio.run(
        client.chat(
            "hello",
            [],
            [],
            conversation_id="local-placeholder",
            transport_session_id="session-1",
            is_start_of_session=True,
            transport_state=transport_state,
        )
    )

    assert text == "hello"
    assert transport_state.conversation_id == "remote-conv-1"
    assert transport_state.session_id == "session-1"
    assert "X-SessionId=session-1" in captured_url["url"]


def test_upload_image_uses_expected_headers_and_form_fields(monkeypatch) -> None:
    client = build_client()
    image = TranslatedImage(
        filename="image.png",
        mime_type="image/png",
        file_extension="png",
        data_url="data:image/png;base64,aGVsbG8=",
        content=b"hello",
    )
    captured: dict[str, object] = {}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, files=None, headers=None):
            captured["url"] = url
            captured["files"] = files
            captured["headers"] = headers
            request = httpx.Request("POST", url)
            return httpx.Response(
                200,
                request=request,
                json={"docId": "doc-123", "fileName": "stored.png", "fileType": ".png"},
            )

    monkeypatch.setattr("m365_copilot_openai_proxy.substrate_client.httpx.AsyncClient", FakeAsyncClient)

    uploaded = asyncio.run(client._upload_image(image, conversation_id="conv-upload"))

    assert uploaded.doc_id == "doc-123"
    assert uploaded.file_name == "image.png"
    assert uploaded.file_type == "png"
    assert uploaded.uploaded_file_name == "stored.png"
    assert captured["url"] == "https://substrate.office.com/m365Copilot/UploadFile"
    assert ("scenario", (None, "UploadImage")) in captured["files"]
    assert ("conversationId", (None, "conv-upload")) in captured["files"]
    assert ("FileBase64", (None, "data:image/png;base64,aGVsbG8=")) in captured["files"]
    assert ("optionsSets", (None, "cwcgptvsan")) in captured["files"]
    assert captured["headers"]["x-scenario"] == "OfficeWebIncludedCopilot"
    assert captured["headers"]["x-variants"] == "feature.EnableImageSupportInUploadFile"
    assert captured["headers"]["Authorization"].startswith("Bearer ")


def test_enterprise_file_upload_uses_graph_upload_session_and_business_doc_id(monkeypatch) -> None:
    client = build_enterprise_snapshot_client(search_access_token="search-token")
    attachment = TranslatedImage(
        kind="file",
        filename="Welcome-to-Copilot-Chat.pdf",
        mime_type="application/pdf",
        file_extension="pdf",
        data_url="data:application/pdf;base64,aGVsbG8=",
        content=b"hello",
    )
    captured: dict[str, object] = {"post_calls": []}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, files=None, headers=None, json=None):
            captured["post_calls"].append(
                {"url": url, "files": files, "headers": headers, "json": json}
            )
            request = httpx.Request("POST", url)
            if "createUploadSession" in url:
                return httpx.Response(
                    200,
                    request=request,
                    json={"uploadUrl": "https://upload.example/business-session"},
                )
            return httpx.Response(200, request=request, json={"ApiVersion": "1.0"})

        async def put(self, url, headers=None, content=None):
            captured["put"] = {"url": url, "headers": headers, "content": content}
            request = httpx.Request("PUT", url)
            return httpx.Response(
                201,
                request=request,
                json={
                    "id": "01HOLHDXPIZMUAOANGN5H26X7S2SEXDZ6F",
                    "name": "Welcome-to-Copilot-Chat.pdf",
                    "webUrl": (
                        "https://pennstateoffice365-my.sharepoint.com/personal/"
                        "tbz5156_psu_edu/Documents/Microsoft%20Copilot%20Chat%20Files/"
                        "Welcome-to-Copilot-Chat.pdf"
                    ),
                    "file": {"fileExtension": ".pdf", "mimeType": "application/pdf"},
                    "parentReference": {
                        "driveType": "business",
                        "driveId": "b!DinXvIuQ9E-GT7RRmDNne8D23_UivmBHlVHJNN-Y4Cp0uSv7xI1JRafDIVNEn6bB",
                        "siteId": "bcd7290e-908b-4ff4-864f-b4519833677b",
                    },
                },
            )

    monkeypatch.setattr("m365_copilot_openai_proxy.substrate_client.httpx.AsyncClient", FakeAsyncClient)

    uploaded = asyncio.run(client._upload_image(attachment, conversation_id="conv-upload"))

    create_call = captured["post_calls"][0]
    unfurl_call = captured["post_calls"][1]
    assert create_call["url"] == (
        "https://graph.microsoft.com/v1.0/me/drive/special/copilotuploads:/Welcome-to-Copilot-Chat.pdf:/createUploadSession"
    )
    assert create_call["json"] == {
        "item": {
            "@microsoft.graph.conflictBehavior": "replace",
            "name": "Welcome-to-Copilot-Chat.pdf",
        }
    }
    assert captured["put"]["url"] == "https://upload.example/business-session"
    assert captured["put"]["headers"]["Content-Range"] == "bytes 0-4/5"
    assert captured["put"]["headers"]["Content-Type"] == "application/octet-stream"
    assert captured["put"]["content"] == b"hello"
    assert unfurl_call["url"] == "https://substrate.office.com/searchservice/api/v1/unfurl?domain=File"
    assert unfurl_call["headers"]["x-anchormailbox"] == (
        "Oid:12345678-1234-1234-1234-1234567890ab@abcdef01-2345-6789-abcd-ef0123456789"
    )
    assert unfurl_call["headers"]["Authorization"] == "Bearer search-token"
    assert unfurl_call["headers"]["x-routingparameter-sessionkey"] == (
        "Oid:12345678-1234-1234-1234-1234567890ab@abcdef01-2345-6789-abcd-ef0123456789"
    )
    assert unfurl_call["headers"]["x-client-language"] == "en-us"
    assert isinstance(unfurl_call["headers"]["client-request-id"], str)
    assert isinstance(unfurl_call["headers"]["client-session-id"], str)
    assert isinstance(unfurl_call["headers"]["x-client-localtime"], str)
    assert unfurl_call["json"]["Scenario"]["Dimensions"][0] == {
        "DimensionName": "ScenarioDescription",
        "DimensionValue": "OfficeWebIncludedCopilot.prefetch.getdocumentsummary.fileupload",
    }
    assert unfurl_call["json"]["EntityRequests"][0]["QueryAnnotations"][0] == {
        "Id": "SPO_YmNkNzI5MGUtOTA4Yi00ZmY0LTg2NGYtYjQ1MTk4MzM2NzdiLGY1ZGZmNmMwLWJlMjItNDc2MC05NTUxLWM5MzRkZjk4ZTAyYSxmYjJiYjk3NC04ZGM0LTQ1NDktYTdjMy0yMTUzNDQ5ZmE2YzE_01HOLHDXPIZMUAOANGN5H26X7S2SEXDZ6F",
        "Type": "LocalFile",
        "Text": "Welcome-to-Copilot-Chat.pdf",
    }
    assert uploaded.kind == "file"
    assert uploaded.doc_id == (
        "SPO_YmNkNzI5MGUtOTA4Yi00ZmY0LTg2NGYtYjQ1MTk4MzM2Nzdi"
        "LGY1ZGZmNmMwLWJlMjItNDc2MC05NTUxLWM5MzRkZjk4ZTAyYS"
        "xmYjJiYjk3NC04ZGM0LTQ1NDktYTdjMy0yMTUzNDQ5ZmE2YzE"
        "_01HOLHDXPIZMUAOANGN5H26X7S2SEXDZ6F"
    )
    assert uploaded.file_name == "Welcome-to-Copilot-Chat.pdf"
    assert uploaded.file_type == "pdf"
    assert uploaded.uploaded_file_name == "Welcome-to-Copilot-Chat.pdf"
    assert uploaded.logical_id is not None
    assert uploaded.annotation_type == "LocalFile"
    assert uploaded.annotation_text == "Welcome-to-Copilot-Chat.pdf"
    assert uploaded.annotation_url == (
        "https://pennstateoffice365-my.sharepoint.com/personal/tbz5156_psu_edu/"
        "Documents/Microsoft%20Copilot%20Chat%20Files/Welcome-to-Copilot-Chat.pdf"
    )


def test_enterprise_file_upload_falls_back_to_access_token_without_search_token(monkeypatch) -> None:
    client = build_enterprise_snapshot_client(search_access_token=None)
    attachment = TranslatedImage(
        kind="file",
        filename="notes.txt",
        mime_type="text/plain",
        file_extension="txt",
        data_url="data:text/plain;base64,aGVsbG8=",
        content=b"hello",
    )
    captured: dict[str, object] = {"post_calls": []}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, files=None, headers=None, json=None):
            captured["post_calls"].append(
                {"url": url, "files": files, "headers": headers, "json": json}
            )
            request = httpx.Request("POST", url)
            if "createUploadSession" in url:
                return httpx.Response(
                    200,
                    request=request,
                    json={"uploadUrl": "https://upload.example/fallback-session"},
                )
            return httpx.Response(200, request=request, json={"ApiVersion": "1.0"})

        async def put(self, url, headers=None, content=None):
            request = httpx.Request("PUT", url)
            return httpx.Response(
                201,
                request=request,
                json={
                    "id": "fallback-item",
                    "name": "notes.txt",
                    "file": {"fileExtension": ".txt", "mimeType": "text/plain"},
                    "parentReference": {
                        "driveType": "business",
                        "driveId": "b!DinXvIuQ9E-GT7RRmDNne8D23_UivmBHlVHJNN-Y4Cp0uSv7xI1JRafDIVNEn6bB",
                    },
                },
            )

    monkeypatch.setattr("m365_copilot_openai_proxy.substrate_client.httpx.AsyncClient", FakeAsyncClient)

    asyncio.run(client._upload_image(attachment, conversation_id="conv-upload"))

    unfurl_call = captured["post_calls"][1]
    assert unfurl_call["headers"]["Authorization"] == f"Bearer {_TEST_JWT}"


def test_personal_upload_image_uses_snapshot_anchor_mailbox(monkeypatch) -> None:
    client = build_personal_client(search_access_token="personal-search-token")
    attachment = TranslatedImage(
        filename="image.png",
        mime_type="image/png",
        file_extension="png",
        data_url="data:image/png;base64,aGVsbG8=",
        content=b"hello",
    )
    captured: dict[str, object] = {"post_calls": []}

    monkeypatch.setattr(
        "m365_copilot_openai_proxy.substrate_client._convert_image_attachment_to_pdf",
        lambda image: TranslatedImage(
            kind="image",
            filename="image.pdf",
            mime_type="application/pdf",
            file_extension="pdf",
            data_url="data:application/pdf;base64,cGRm",
            content=b"pdf",
        ),
    )

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, files=None, headers=None, json=None):
            captured["post_calls"].append(
                {"url": url, "files": files, "headers": headers, "json": json}
            )
            request = httpx.Request("POST", url)
            if "createUploadSession" in url:
                return httpx.Response(
                    200,
                    request=request,
                    json={"uploadUrl": "https://upload.example/session"},
                )
            return httpx.Response(200, request=request, json={"ApiVersion": "1.0"})

        async def put(self, url, headers=None, content=None):
            captured["put"] = {"url": url, "headers": headers, "content": content}
            request = httpx.Request("PUT", url)
            return httpx.Response(
                201,
                request=request,
                json={
                    "id": "853E527A6BF3C11E!s45b7",
                    "name": "image.pdf",
                    "file": {"fileExtension": ".pdf", "mimeType": "application/pdf"},
                    "parentReference": {"driveId": "853E527A6BF3C11E"},
                },
            )

    monkeypatch.setattr("m365_copilot_openai_proxy.substrate_client.httpx.AsyncClient", FakeAsyncClient)

    uploaded = asyncio.run(client._upload_image(attachment, conversation_id="86ac4933-eab7-44ea-aec5-5362e289f849"))

    create_call = captured["post_calls"][0]
    unfurl_call = captured["post_calls"][1]
    assert create_call["url"] == (
        "https://graph.microsoft.com/v1.0/me/drive/special/copilotuploads:/image.pdf:/createUploadSession"
    )
    assert create_call["json"] == {
        "item": {"@microsoft.graph.conflictBehavior": "replace", "name": "image.pdf"}
    }
    assert captured["put"]["url"] == "https://upload.example/session"
    assert captured["put"]["headers"]["Content-Range"] == "bytes 0-2/3"
    assert captured["put"]["headers"]["Content-Type"] == "application/octet-stream"
    assert captured["put"]["content"] == b"pdf"
    assert unfurl_call["url"] == "https://substrate.office.com/searchservice/api/v1/unfurl?domain=File"
    assert unfurl_call["headers"]["x-anchormailbox"] == (
        "Oid:00000000-0000-0000-853e-527a6bf3c11e@84df9e7f-e9f6-40af-b435-aaaaaaaaaaaa"
    )
    assert unfurl_call["headers"]["Authorization"] == "Bearer personal-search-token"
    assert unfurl_call["headers"]["x-routingparameter-sessionkey"] == (
        "Oid:00000000-0000-0000-853e-527a6bf3c11e@84df9e7f-e9f6-40af-b435-aaaaaaaaaaaa"
    )
    assert unfurl_call["headers"]["x-client-language"] == "en-us"
    assert isinstance(unfurl_call["headers"]["client-request-id"], str)
    assert isinstance(unfurl_call["headers"]["client-session-id"], str)
    assert isinstance(unfurl_call["headers"]["x-client-localtime"], str)
    assert unfurl_call["json"]["Scenario"]["Dimensions"][0] == {
        "DimensionName": "ScenarioDescription",
        "DimensionValue": "OfficeWebPremiumConsumerCopilot.prefetch.getdocumentsummary.fileupload",
    }
    assert unfurl_call["json"]["Cvid"] == "86ac4933-eab7-44ea-aec5-5362e289f849"
    assert unfurl_call["json"]["EntityRequests"][0]["QueryAnnotations"][0] == {
        "Id": "SPO_853E527A6BF3C11E_853E527A6BF3C11E!s45b7",
        "Type": "LocalFile",
        "Text": "image.pdf",
    }
    assert uploaded.kind == "image"
    assert uploaded.doc_id == "SPO_853E527A6BF3C11E_853E527A6BF3C11E!s45b7"
    assert uploaded.file_name == "image.pdf"
    assert uploaded.file_type == "pdf"
    assert uploaded.uploaded_file_name == "image.pdf"
    assert uploaded.logical_id is not None
    assert uploaded.annotation_type == "LocalFile"
    assert uploaded.annotation_text == "image.pdf"
    assert uploaded.annotation_url == (
        "https://onedrive.live.com?cid=853E527A6BF3C11E&id=853E527A6BF3C11E!s45b7"
    )
