from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from .config import Settings
from .models import HistoryTurn, TranslatedAttachment, TranslatedRequest


def _normalize_scope_user(scope_user: str | None) -> str:
    return (scope_user or "").strip()


def _history_hash_payload(system_text: str, turns: list[HistoryTurn]) -> dict[str, object]:
    return {
        "system": system_text,
        "turns": [{"role": turn.role, "text": turn.text} for turn in turns],
    }


def compute_history_hash(system_text: str, turns: list[HistoryTurn]) -> str:
    payload = json.dumps(
        _history_hash_payload(system_text, turns),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def compute_prior_history_hash(translated: TranslatedRequest) -> str:
    return compute_history_hash(translated.system_text, translated.prior_turns)


def compute_advanced_history_hash(translated: TranslatedRequest, assistant_text: str) -> str:
    advanced_turns = list(translated.prior_turns)
    advanced_turns.append(HistoryTurn(role="user", text=translated.prompt))
    advanced_turns.append(HistoryTurn(role="assistant", text=assistant_text))
    return compute_history_hash(translated.system_text, advanced_turns)


@dataclass(slots=True)
class ReservedConversation:
    conversation_id: str
    current_history_hash: str


@dataclass(slots=True)
class PreparedConversationTurn:
    conversation_id: str
    is_start_of_session: bool
    routing_mode: str
    prompt: str
    additional_context: list[str]
    attachments: list[TranslatedAttachment]
    translated: TranslatedRequest
    scope_user: str | None
    reserved_conversation_id: str | None = None


class ConversationHistoryStore:
    def __init__(self, db_path: str | Path, max_conversations: int = 500):
        self._db_path = Path(db_path)
        self._max_conversations = max(1, max_conversations)
        self._lock = threading.Lock()
        self._initialized = False
        self._leases: set[str] = set()

    def try_reserve(self, scope_user: str | None, history_hash: str) -> ReservedConversation | None:
        normalized_scope = _normalize_scope_user(scope_user)
        with self._lock:
            self._initialize_locked()
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT conversation_id, current_history_hash
                    FROM conversations
                    WHERE scope_user = ? AND current_history_hash = ?
                    """,
                    (normalized_scope, history_hash),
                ).fetchone()
            if row is None:
                return None
            conversation_id = str(row["conversation_id"])
            if conversation_id in self._leases:
                return None
            self._leases.add(conversation_id)
            return ReservedConversation(
                conversation_id=conversation_id,
                current_history_hash=str(row["current_history_hash"]),
            )

    def release(self, conversation_id: str | None) -> None:
        if not conversation_id:
            return
        with self._lock:
            self._leases.discard(conversation_id)

    def advance(
        self,
        scope_user: str | None,
        conversation_id: str,
        new_history_hash: str,
    ) -> None:
        normalized_scope = _normalize_scope_user(scope_user)
        now = time.time()
        with self._lock:
            self._initialize_locked()
            with self._connect() as conn:
                conn.execute(
                    """
                    DELETE FROM conversations
                    WHERE scope_user = ? AND current_history_hash = ? AND conversation_id <> ?
                    """,
                    (normalized_scope, new_history_hash, conversation_id),
                )
                conn.execute(
                    """
                    INSERT INTO conversations (
                        conversation_id,
                        scope_user,
                        current_history_hash,
                        turn_count,
                        created_at,
                        updated_at
                    ) VALUES (?, ?, ?, 1, ?, ?)
                    ON CONFLICT(conversation_id) DO UPDATE SET
                        scope_user = excluded.scope_user,
                        current_history_hash = excluded.current_history_hash,
                        turn_count = conversations.turn_count + 1,
                        updated_at = excluded.updated_at
                    """,
                    (conversation_id, normalized_scope, new_history_hash, now, now),
                )
                self._evict_locked(conn)

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize_locked(self) -> None:
        if self._initialized:
            return
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id TEXT PRIMARY KEY,
                    scope_user TEXT NOT NULL,
                    current_history_hash TEXT NOT NULL,
                    turn_count INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_conversations_scope_history
                ON conversations(scope_user, current_history_hash)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_conversations_updated_at
                ON conversations(updated_at)
                """
            )
        self._initialized = True

    def _evict_locked(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            SELECT conversation_id
            FROM conversations
            ORDER BY updated_at DESC
            """
        ).fetchall()
        if len(rows) <= self._max_conversations:
            return
        removable_ids: list[str] = []
        for row in reversed(rows):
            conversation_id = str(row["conversation_id"])
            if conversation_id in self._leases:
                continue
            removable_ids.append(conversation_id)
            if len(rows) - len(removable_ids) <= self._max_conversations:
                break
        for conversation_id in removable_ids:
            conn.execute(
                "DELETE FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            )


class ConversationReuseService:
    def __init__(self, settings: Settings):
        self._enabled = settings.enable_conversation_reuse
        self._store = ConversationHistoryStore(
            settings.conversation_db_path,
            max_conversations=settings.conversation_max_conversations,
        )

    def prepare_turn(
        self,
        translated: TranslatedRequest,
        *,
        scope_user: str | None,
    ) -> PreparedConversationTurn:
        if not self._enabled:
            return PreparedConversationTurn(
                conversation_id=str(uuid.uuid4()),
                is_start_of_session=True,
                routing_mode="stateless_disabled",
                prompt=translated.prompt,
                additional_context=translated.additional_context,
                attachments=translated.attachments,
                translated=translated,
                scope_user=scope_user,
            )

        prior_hash = compute_prior_history_hash(translated)
        reserved = self._store.try_reserve(scope_user, prior_hash)
        if reserved is None:
            return PreparedConversationTurn(
                conversation_id=str(uuid.uuid4()),
                is_start_of_session=True,
                routing_mode="stateless_miss",
                prompt=translated.prompt,
                additional_context=translated.additional_context,
                attachments=translated.attachments,
                translated=translated,
                scope_user=scope_user,
            )

        return PreparedConversationTurn(
            conversation_id=reserved.conversation_id,
            is_start_of_session=False,
            routing_mode="reused",
            prompt=translated.prompt,
            additional_context=[],
            attachments=translated.current_attachments,
            translated=translated,
            scope_user=scope_user,
            reserved_conversation_id=reserved.conversation_id,
        )

    def complete_turn(self, turn: PreparedConversationTurn, assistant_text: str) -> None:
        if not self._enabled:
            return
        try:
            new_history_hash = compute_advanced_history_hash(turn.translated, assistant_text)
            self._store.advance(turn.scope_user, turn.conversation_id, new_history_hash)
        finally:
            self._store.release(turn.reserved_conversation_id)

    def abort_turn(self, turn: PreparedConversationTurn) -> None:
        if not self._enabled:
            return
        self._store.release(turn.reserved_conversation_id)
