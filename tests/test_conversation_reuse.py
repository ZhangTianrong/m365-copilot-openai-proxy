from __future__ import annotations

from m365_copilot_openai_proxy.config import Settings
from m365_copilot_openai_proxy.conversation_reuse import ConversationHistoryStore, ConversationReuseService
from m365_copilot_openai_proxy.models import TranslatedAttachment, TranslatedRequest


def test_history_store_reserves_and_releases_latest_key(tmp_path) -> None:
    store = ConversationHistoryStore(tmp_path / "conversation_reuse.db", max_conversations=10)
    store.advance("alice", "conv-1", "hash-1")

    first = store.try_reserve("alice", "hash-1")
    second = store.try_reserve("alice", "hash-1")

    assert first is not None
    assert first.conversation_id == "conv-1"
    assert second is None

    store.release("conv-1")
    third = store.try_reserve("alice", "hash-1")
    assert third is not None


def test_history_store_evicts_least_recently_used_rows(tmp_path) -> None:
    db_path = tmp_path / "conversation_reuse.db"
    store = ConversationHistoryStore(db_path, max_conversations=2)
    store.advance(None, "conv-1", "hash-1")
    store.advance(None, "conv-2", "hash-2")
    store.advance(None, "conv-3", "hash-3")

    assert store.try_reserve(None, "hash-1") is None
    assert store.try_reserve(None, "hash-2") is not None
    store.release("conv-2")
    assert store.try_reserve(None, "hash-3") is not None


def test_reuse_service_allows_enterprise_requests_with_attachments(tmp_path) -> None:
    service = ConversationReuseService(
        Settings(
            _env_file=None,
            M365_ACCOUNT_MODE="enterprise",
            M365_ENABLE_CONVERSATION_REUSE=True,
            M365_CONVERSATION_DB_PATH=str(tmp_path / "conversation_reuse.db"),
        )
    )
    translated = TranslatedRequest(
        prompt="What is this?",
        additional_context=["Prior conversation transcript:\nUser: Earlier"],
        attachments=[
            TranslatedAttachment(
                filename="image.png",
                mime_type="image/png",
                file_extension="png",
                data_url="data:image/png;base64,aGVsbG8=",
                content=b"hello",
            )
        ],
        current_attachments=[
            TranslatedAttachment(
                filename="image.png",
                mime_type="image/png",
                file_extension="png",
                data_url="data:image/png;base64,aGVsbG8=",
                content=b"hello",
            )
        ],
    )

    first = service.prepare_turn(translated, scope_user="alice")

    assert first.routing_mode == "stateless_miss"
    assert first.is_start_of_session is True
    assert first.attachments == translated.attachments

    service.complete_turn(first, "first reply")

    follow_up = TranslatedRequest(
        prompt="Follow up",
        prior_turns=[
            {"role": "user", "text": "What is this?"},
            {"role": "assistant", "text": "first reply"},
        ],
        attachments=translated.attachments,
        current_attachments=translated.current_attachments,
    )
    second = service.prepare_turn(follow_up, scope_user="alice")

    assert second.routing_mode == "reused"
    assert second.is_start_of_session is False
    assert second.conversation_id == first.conversation_id
    assert second.transport_session_id is None


def test_reuse_service_allows_personal_requests_with_attachments(tmp_path) -> None:
    service = ConversationReuseService(
        Settings(
            _env_file=None,
            M365_ACCOUNT_MODE="personal",
            M365_ENABLE_CONVERSATION_REUSE=True,
            M365_CONVERSATION_DB_PATH=str(tmp_path / "conversation_reuse.db"),
        )
    )
    translated = TranslatedRequest(
        prompt="What is this?",
        attachments=[
            TranslatedAttachment(
                filename="image.png",
                mime_type="image/png",
                file_extension="png",
                data_url="data:image/png;base64,aGVsbG8=",
                content=b"hello",
            )
        ],
        current_attachments=[
            TranslatedAttachment(
                filename="image.png",
                mime_type="image/png",
                file_extension="png",
                data_url="data:image/png;base64,aGVsbG8=",
                content=b"hello",
            )
        ],
    )

    first = service.prepare_turn(translated, scope_user="alice")

    assert first.routing_mode == "stateless_miss"
    assert first.is_start_of_session is True
    assert first.transport_session_id is not None

    service.complete_turn(
        first,
        "first reply",
        resolved_conversation_id="remote-conv-1",
        resolved_transport_session_id=first.transport_session_id,
    )

    follow_up = TranslatedRequest(
        prompt="Follow up",
        prior_turns=[
            {"role": "user", "text": "What is this?"},
            {"role": "assistant", "text": "first reply"},
        ],
        attachments=translated.attachments,
        current_attachments=translated.current_attachments,
    )
    second = service.prepare_turn(follow_up, scope_user="alice")

    assert second.routing_mode == "reused"
    assert second.is_start_of_session is False
    assert second.conversation_id == "remote-conv-1"
    assert second.transport_session_id == first.transport_session_id
