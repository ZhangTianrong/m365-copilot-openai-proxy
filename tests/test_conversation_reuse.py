from __future__ import annotations

from m365_copilot_openai_proxy.conversation_reuse import ConversationHistoryStore


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
