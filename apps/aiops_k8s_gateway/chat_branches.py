"""Immutable message branching inside the Gateway-owned Chat Session Module."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

from .chat_streaming import complete_response

from .chat_sessions import (
    JSON,
    Responder,
    ChatError,
    _canonical,
    _chat_result,
    _hash,
    _required,
    _safe_content,
    _safe_scope,
)
from .gateway_db import register_migrations

if TYPE_CHECKING:
    from .chat_sessions import ChatSessions


_SCHEMA_VERSION = 52
_SCHEMA = """
ALTER TABLE chat_messages RENAME TO chat_messages_legacy;
CREATE TABLE chat_messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    status TEXT NOT NULL CHECK (status IN ('sending', 'completed', 'failed')),
    position INTEGER NOT NULL CHECK (position > 0),
    content TEXT NOT NULL,
    parent_id TEXT REFERENCES chat_messages(id),
    reply_to_id TEXT REFERENCES chat_messages(id),
    idempotency_key TEXT,
    request_hash TEXT,
    error_code TEXT,
    scope_json TEXT,
    result_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(session_id, idempotency_key),
    UNIQUE(session_id, position)
);
INSERT INTO chat_messages (
    id, session_id, role, status, position, content, parent_id, reply_to_id,
    idempotency_key, request_hash, error_code, scope_json, result_json, created_at, updated_at
)
SELECT
    legacy.id, legacy.session_id, legacy.role, legacy.status, legacy.position, legacy.content,
    CASE
        WHEN legacy.role = 'assistant' THEN legacy.reply_to_id
        ELSE (
            SELECT previous.id FROM chat_messages_legacy previous
            WHERE previous.session_id = legacy.session_id AND previous.position = legacy.position - 1
        )
    END,
    legacy.reply_to_id, legacy.idempotency_key, legacy.request_hash, legacy.error_code,
    legacy.scope_json, legacy.result_json, legacy.created_at, legacy.updated_at
FROM chat_messages_legacy legacy
ORDER BY legacy.session_id, legacy.position;
DROP TABLE chat_messages_legacy;
CREATE INDEX chat_messages_session ON chat_messages(session_id, created_at, id);
CREATE INDEX chat_messages_parent ON chat_messages(session_id, parent_id, role, position);
ALTER TABLE chat_sessions ADD COLUMN current_head_id TEXT REFERENCES chat_messages(id);
UPDATE chat_sessions
SET current_head_id = (
    SELECT messages.id FROM chat_messages messages
    WHERE messages.session_id = chat_sessions.id
    ORDER BY messages.position DESC LIMIT 1
);
CREATE TABLE chat_branch_operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    operation TEXT NOT NULL CHECK (operation IN ('edit', 'reload', 'switch')),
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
    source_message_id TEXT NOT NULL,
    created_message_id TEXT,
    response_json TEXT,
    created_at REAL NOT NULL,
    UNIQUE(owner_id, idempotency_key)
);
CREATE INDEX chat_branch_operations_session ON chat_branch_operations(owner_id, session_id, created_at DESC);
"""
register_migrations(((_SCHEMA_VERSION, _SCHEMA),))


class ChatBranches:
    """Own edit, reload, switch, and branch ancestry behavior."""

    def __init__(self, sessions: ChatSessions) -> None:
        self._sessions = sessions

    def edit(
        self,
        owner_id: str,
        session_id: str,
        message_id: str,
        *,
        content: str,
        idempotency_key: str,
        respond: Responder,
        scope: JSON | None = None,
        stream: bool = False,
    ) -> JSON:
        owner_id = _required(owner_id, "owner_id", 200)
        session_id = _required(session_id, "session_id", 200)
        message_id = _required(message_id, "message_id", 200)
        raw_content = _required(content, "content", 8_000)
        content = _safe_content(raw_content)
        idempotency_key = _required(idempotency_key, "idempotency_key", 200)
        now = self._sessions._clock()
        request_hash = _hash({"message_id": message_id, "content": raw_content, "scope": scope})
        with self._sessions._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            replay = self._reserve(conn, owner_id, session_id, "edit", message_id, idempotency_key, request_hash, now)
            if replay is not None:
                conn.commit()
                return replay
            session_row = self._sessions._owned_in(conn, owner_id, session_id)
            self.assert_idle(conn, session_id, session_row["current_head_id"])
            if message_id not in self.ids_in(conn, session_id, session_row["current_head_id"]):
                raise ChatError("chat_message_not_found", "Chat message not found")
            source = conn.execute(
                "SELECT * FROM chat_messages WHERE id = ? AND session_id = ? AND role = 'user' AND status = 'completed'",
                (message_id, session_id),
            ).fetchone()
            if source is None:
                raise ChatError("chat_message_not_found", "Chat message not found")
            frozen_scope = _safe_scope(scope) if scope is not None else (
                json.loads(str(source["scope_json"])) if source["scope_json"] else None
            )
            position = int(conn.execute(
                "SELECT COALESCE(MAX(position), 0) FROM chat_messages WHERE session_id = ?", (session_id,)
            ).fetchone()[0])
            user_id, assistant_id = self._sessions._id_factory(), self._sessions._id_factory()
            conn.execute(
                """INSERT INTO chat_messages
                   (id, session_id, role, status, position, content, parent_id, request_hash, scope_json, created_at, updated_at)
                   VALUES (?, ?, 'user', 'completed', ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, session_id, position + 1, content, source["parent_id"], request_hash, _canonical(frozen_scope), now, now),
            )
            conn.execute(
                """INSERT INTO chat_messages
                   (id, session_id, role, status, position, content, parent_id, reply_to_id, scope_json, created_at, updated_at)
                   VALUES (?, ?, 'assistant', 'sending', ?, '', ?, ?, ?, ?, ?)""",
                (assistant_id, session_id, position + 2, user_id, user_id, _canonical(frozen_scope), now, now),
            )
            self._activate(conn, owner_id, session_id, idempotency_key, assistant_id, frozen_scope, now)
            self._sessions._append_event(conn, session_id, "message.created", f"message:{user_id}", {"message_id": user_id}, now)
            self._sessions._append_event(conn, session_id, "message.created", f"message:{assistant_id}", {"message_id": assistant_id}, now)
            self._sessions._append_event(
                conn, session_id, "branch.created", f"branch:edit:{idempotency_key}",
                {"operation": "edit", "source_message_id": message_id, "message_ids": [user_id, assistant_id], "head_id": assistant_id}, now,
            )
            conn.commit()
        return self._respond(owner_id, session_id, assistant_id, frozen_scope, respond, idempotency_key, stream)

    def reload(
        self,
        owner_id: str,
        session_id: str,
        message_id: str,
        *,
        idempotency_key: str,
        respond: Responder,
        stream: bool = False,
    ) -> JSON:
        owner_id = _required(owner_id, "owner_id", 200)
        session_id = _required(session_id, "session_id", 200)
        message_id = _required(message_id, "message_id", 200)
        idempotency_key = _required(idempotency_key, "idempotency_key", 200)
        now = self._sessions._clock()
        request_hash = _hash({"message_id": message_id})
        with self._sessions._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            replay = self._reserve(conn, owner_id, session_id, "reload", message_id, idempotency_key, request_hash, now)
            if replay is not None:
                conn.commit()
                return replay
            session_row = self._sessions._owned_in(conn, owner_id, session_id)
            self.assert_idle(conn, session_id, session_row["current_head_id"])
            if message_id not in self.ids_in(conn, session_id, session_row["current_head_id"]):
                raise ChatError("chat_message_not_found", "Chat message not found")
            source = conn.execute(
                "SELECT * FROM chat_messages WHERE id = ? AND session_id = ? AND role = 'assistant' AND status = 'completed'",
                (message_id, session_id),
            ).fetchone()
            if source is None:
                raise ChatError("chat_message_not_found", "Chat message not found")
            position = int(conn.execute(
                "SELECT COALESCE(MAX(position), 0) FROM chat_messages WHERE session_id = ?", (session_id,)
            ).fetchone()[0])
            assistant_id = self._sessions._id_factory()
            conn.execute(
                """INSERT INTO chat_messages
                   (id, session_id, role, status, position, content, parent_id, reply_to_id, request_hash, scope_json, created_at, updated_at)
                   VALUES (?, ?, 'assistant', 'sending', ?, '', ?, ?, ?, ?, ?, ?)""",
                (assistant_id, session_id, position + 1, source["parent_id"], source["reply_to_id"], source["request_hash"], source["scope_json"], now, now),
            )
            frozen_scope = json.loads(str(source["scope_json"])) if source["scope_json"] else None
            self._activate(conn, owner_id, session_id, idempotency_key, assistant_id, frozen_scope, now)
            self._sessions._append_event(conn, session_id, "message.created", f"message:{assistant_id}", {"message_id": assistant_id}, now)
            self._sessions._append_event(
                conn, session_id, "branch.created", f"branch:reload:{idempotency_key}",
                {"operation": "reload", "source_message_id": message_id, "message_ids": [assistant_id], "head_id": assistant_id}, now,
            )
            conn.commit()
        return self._respond(owner_id, session_id, assistant_id, frozen_scope, respond, idempotency_key, stream)

    def switch_branch(
        self,
        owner_id: str,
        session_id: str,
        message_id: str,
        *,
        idempotency_key: str,
    ) -> JSON:
        owner_id = _required(owner_id, "owner_id", 200)
        session_id = _required(session_id, "session_id", 200)
        message_id = _required(message_id, "message_id", 200)
        idempotency_key = _required(idempotency_key, "idempotency_key", 200)
        now = self._sessions._clock()
        request_hash = _hash({"message_id": message_id})
        with self._sessions._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            replay = self._reserve(conn, owner_id, session_id, "switch", message_id, idempotency_key, request_hash, now)
            if replay is not None:
                conn.commit()
                return replay
            session_row = self._sessions._owned_in(conn, owner_id, session_id)
            self.assert_idle(conn, session_id, session_row["current_head_id"])
            target = conn.execute(
                "SELECT * FROM chat_messages WHERE id = ? AND session_id = ? AND role = 'assistant' AND status = 'completed'",
                (message_id, session_id),
            ).fetchone()
            if target is None:
                raise ChatError("chat_message_not_found", "Chat message not found")
            self.assert_idle(conn, session_id, message_id)
            conn.execute(
                "UPDATE chat_sessions SET current_head_id = ?, last_scope_json = ?, updated_at = ? WHERE id = ?",
                (message_id, target["scope_json"], now, session_id),
            )
            self._sessions._append_event(
                conn, session_id, "branch.switched", f"branch:switch:{idempotency_key}",
                {"message_id": message_id, "head_id": message_id}, now,
            )
            response = self._sessions._get_in(conn, owner_id, session_id)
            conn.execute(
                "UPDATE chat_branch_operations SET created_message_id = ?, response_json = ? WHERE owner_id = ? AND idempotency_key = ?",
                (message_id, _canonical(response), owner_id, idempotency_key),
            )
            conn.commit()
            return response

    def conversation(self, owner_id: str, session_id: str, head_id: str, *, include_ids: bool = False) -> list[dict[str, str]]:
        with self._sessions._database.connect() as conn:
            self._sessions._owned_in(conn, owner_id, session_id)
            rows = conn.execute("SELECT * FROM chat_messages WHERE session_id = ?", (session_id,)).fetchall()
            by_id = {str(row["id"]): row for row in rows}
            visible: list[sqlite3.Row] = []
            cursor = head_id
            while cursor:
                row = by_id.get(cursor)
                if row is None:
                    break
                visible.append(row)
                cursor = str(row["parent_id"]) if row["parent_id"] is not None else ""
            visible.reverse()
            return [
                {
                    "role": str(row["role"]), "content": str(row["content"]),
                    **({"message_id": str(row["id"])} if include_ids else {}),
                }
                for row in visible if row["status"] == "completed"
            ]

    @staticmethod
    def ids_in(conn: sqlite3.Connection, session_id: str, head_id: object) -> set[str]:
        if not isinstance(head_id, str) or not head_id:
            return set()
        rows = conn.execute("SELECT id, parent_id FROM chat_messages WHERE session_id = ?", (session_id,)).fetchall()
        by_id = {str(row["id"]): row for row in rows}
        visible: set[str] = set()
        cursor = head_id
        while cursor and cursor not in visible:
            row = by_id.get(cursor)
            if row is None:
                break
            visible.add(cursor)
            cursor = str(row["parent_id"]) if row["parent_id"] is not None else ""
        return visible

    def assert_idle(self, conn: sqlite3.Connection, session_id: str, head_id: object) -> None:
        ids = self.ids_in(conn, session_id, head_id)
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        if conn.execute(
            f"SELECT 1 FROM chat_messages WHERE session_id = ? AND id IN ({placeholders}) AND status = 'sending' LIMIT 1",
            (session_id, *sorted(ids)),
        ).fetchone() is not None:
            raise ChatError("message_running", "Chat branch has a running response")

    def _reserve(
        self,
        conn: sqlite3.Connection,
        owner_id: str,
        session_id: str,
        operation: str,
        source_message_id: str,
        idempotency_key: str,
        request_hash: str,
        now: float,
    ) -> JSON | None:
        self._sessions._owned_in(conn, owner_id, session_id)
        row = conn.execute(
            "SELECT request_hash, response_json FROM chat_branch_operations WHERE owner_id = ? AND idempotency_key = ?",
            (owner_id, idempotency_key),
        ).fetchone()
        if row is not None:
            if str(row["request_hash"]) != request_hash:
                raise ChatError("idempotency_conflict", "Chat branch request conflicts with an accepted request")
            if row["response_json"] is None:
                raise ChatError("message_running", "Chat branch operation is still running")
            return json.loads(str(row["response_json"]))
        conn.execute(
            """INSERT INTO chat_branch_operations
               (owner_id, session_id, operation, idempotency_key, request_hash, source_message_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (owner_id, session_id, operation, idempotency_key, request_hash, source_message_id, now),
        )
        return None

    @staticmethod
    def _activate(
        conn: sqlite3.Connection,
        owner_id: str,
        session_id: str,
        idempotency_key: str,
        assistant_id: str,
        frozen_scope: JSON | None,
        now: float,
    ) -> None:
        conn.execute(
            "UPDATE chat_branch_operations SET created_message_id = ? WHERE owner_id = ? AND idempotency_key = ?",
            (assistant_id, owner_id, idempotency_key),
        )
        conn.execute(
            "UPDATE chat_sessions SET last_scope_json = ?, current_head_id = ?, updated_at = ? WHERE id = ?",
            (_canonical(frozen_scope), assistant_id, now, session_id),
        )

    def _respond(
        self,
        owner_id: str,
        session_id: str,
        assistant_id: str,
        frozen_scope: JSON | None,
        respond: Responder,
        idempotency_key: str,
        stream: bool,
    ) -> JSON:
        try:
            result = _chat_result(respond(self._sessions._model_request(owner_id, session_id, assistant_id, frozen_scope)), frozen_scope)
        except ChatError as exc:
            content, code = (
                (exc.message, exc.code)
                if exc.code == "image_input_unsupported"
                else ("暂时无法回答，请重试。", "model_unavailable")
            )
            self._sessions._finish(
                owner_id, session_id, assistant_id, status="failed",
                content=content, error_code=code, result=None,
            )
        except Exception:
            self._sessions._finish(owner_id, session_id, assistant_id, status="failed", content="暂时无法回答，请重试。", error_code="model_unavailable", result=None)
        else:
            complete_response(self._sessions, owner_id, session_id, assistant_id, result, stream=stream)
        with self._sessions._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            response = self._sessions._get_in(conn, owner_id, session_id)
            conn.execute(
                "UPDATE chat_branch_operations SET response_json = ? WHERE owner_id = ? AND idempotency_key = ? AND response_json IS NULL",
                (_canonical(response), owner_id, idempotency_key),
            )
            conn.commit()
            return response
