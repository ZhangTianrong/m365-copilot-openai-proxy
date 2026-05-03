from __future__ import annotations

import asyncio
import json

import httpx

from m365_copilot_openai_proxy.models import TranslatedImage, UploadedImage
from m365_copilot_openai_proxy.substrate_client import SIGNALR_SEP, SubstrateCopilotClient

_TEST_JWT = (
    "eyJhbGciOiJub25lIn0."
    "eyJvaWQiOiIxMjM0NTY3OC0xMjM0LTEyMzQtMTIzNC0xMjM0NTY3ODkwYWIiLCJ0aWQiOiJhYmNkZWYwMS0yMzQ1LTY3ODktYWJjZC1lZjAxMjM0NTY3ODkiLCJleHAiOjQxMDAwMDAwMDB9."
)


def build_client() -> SubstrateCopilotClient:
    return SubstrateCopilotClient(_TEST_JWT, "America/New_York")


def test_chat_invoke_includes_image_annotations() -> None:
    client = build_client()
    uploaded_images = [
        UploadedImage(doc_id="doc-1", file_name="image.png", file_type="png"),
        UploadedImage(doc_id="doc-2", file_name="image-2.jpg", file_type="jpg"),
    ]

    payload = client._chat_invoke("describe", "conv-1", "session-1", "req-1", uploaded_images)
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
