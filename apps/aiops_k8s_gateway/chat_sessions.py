"""Gateway-owned private Chat Sessions and durable message events."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Callable

from aiops.contracts.governed_skills import normalize_skill_versions

from .chat_attachments import ChatAttachments
from .chat_content_safety import SENSITIVE_KEY as _SENSITIVE_KEY, redact
from .gateway_db import GatewayDatabase, register_migrations


JSON = dict[str, object]
Responder = Callable[[JSON], JSON]
ScopeRefreezer = Callable[[JSON], JSON]
_RETENTION_SECONDS = 30 * 24 * 60 * 60
_SCHEMA_VERSION = 45
_SCHEMA = """
CREATE TABLE chat_sessions (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    title TEXT NOT NULL,
    create_key TEXT NOT NULL,
    create_hash TEXT NOT NULL CHECK (length(create_hash) = 64),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    expires_at REAL NOT NULL CHECK (expires_at > created_at),
    UNIQUE(owner_id, create_key)
);
CREATE INDEX chat_sessions_owner ON chat_sessions(owner_id, updated_at DESC);
CREATE INDEX chat_sessions_expiry ON chat_sessions(expires_at);

CREATE TABLE chat_messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    status TEXT NOT NULL CHECK (status IN ('sending', 'completed', 'failed')),
    position INTEGER NOT NULL CHECK (position > 0),
    content TEXT NOT NULL,
    reply_to_id TEXT REFERENCES chat_messages(id),
    idempotency_key TEXT,
    request_hash TEXT,
    error_code TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(session_id, idempotency_key),
    UNIQUE(session_id, position),
    UNIQUE(reply_to_id)
);
CREATE INDEX chat_messages_session ON chat_messages(session_id, created_at, id);

CREATE TABLE chat_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(session_id, idempotency_key)
);
CREATE INDEX chat_events_session ON chat_events(session_id, event_id);
"""
_RESULT_SCHEMA_VERSION = 46
_RESULT_SCHEMA = """
ALTER TABLE chat_sessions ADD COLUMN last_scope_json TEXT;
ALTER TABLE chat_messages ADD COLUMN scope_json TEXT;
ALTER TABLE chat_messages ADD COLUMN result_json TEXT;
"""
_MANAGEMENT_SCHEMA_VERSION = 51
_MANAGEMENT_SCHEMA = """
ALTER TABLE chat_sessions ADD COLUMN title_manual INTEGER NOT NULL DEFAULT 0 CHECK (title_manual IN (0, 1));
ALTER TABLE chat_sessions ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1));
ALTER TABLE chat_sessions ADD COLUMN archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1));
CREATE TABLE chat_session_mutations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    action TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
    before_json TEXT,
    response_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(owner_id, idempotency_key)
);
CREATE INDEX chat_session_mutations_session ON chat_session_mutations(owner_id, session_id, created_at DESC);
"""
register_migrations((
    (_SCHEMA_VERSION, _SCHEMA),
    (_RESULT_SCHEMA_VERSION, _RESULT_SCHEMA),
    (_MANAGEMENT_SCHEMA_VERSION, _MANAGEMENT_SCHEMA),
))


class ChatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ChatSessions:
    """Own private Session/message retention and observable Chat state."""

    def __init__(
        self,
        database: GatewayDatabase | Path | str,
        *,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        attachment_source: ChatAttachments | None = None,
    ) -> None:
        self._database = database if isinstance(database, GatewayDatabase) else GatewayDatabase(database)
        self._clock = clock
        self._id_factory = id_factory
        self._attachment_source = attachment_source
        from .chat_branches import ChatBranches
        self._branches = ChatBranches(self)

    def create(self, owner_id: str, *, idempotency_key: str) -> JSON:
        owner_id = _required(owner_id, "owner_id", 200)
        idempotency_key = _required(idempotency_key, "idempotency_key", 200)
        now = self._clock()
        with self._database.connect() as conn:
            self._purge_in(conn, now)
            row = conn.execute(
                "SELECT id, create_hash FROM chat_sessions WHERE owner_id = ? AND create_key = ?",
                (owner_id, idempotency_key),
            ).fetchone()
            request_hash = _hash({})
            if row is not None:
                if str(row["create_hash"]) != request_hash:
                    raise ChatError("idempotency_conflict", "Chat Session request conflicts with an accepted request")
                session_id = str(row["id"])
            else:
                session_id = self._id_factory()
                conn.execute(
                    """INSERT INTO chat_sessions
                       (id, owner_id, title, create_key, create_hash, created_at, updated_at, expires_at)
                       VALUES (?, ?, '新对话', ?, ?, ?, ?, ?)""",
                    (session_id, owner_id, idempotency_key, request_hash, now, now, now + _RETENTION_SECONDS),
                )
                self._append_event(conn, session_id, "session.created", f"session:{session_id}", {"session_id": session_id}, now)
        return self.get(owner_id, session_id)

    def list(self, owner_id: str, *, query: str = "", filter: str = "all", attachment_session_ids: set[str] | None = None) -> list[JSON]:
        owner_id = _required(owner_id, "owner_id", 200)
        query = _required(query, "query", 200) if query else ""
        if filter not in {"all", "normal", "pinned", "archived"}:
            raise ChatError("invalid_filter", "Chat Session filter is invalid")
        with self._database.connect() as conn:
            where = ["s.owner_id = ?"]
            params: list[object] = [owner_id]
            if filter == "archived":
                where.append("s.archived = 1")
            elif filter in {"normal", "pinned"} or not query:
                where.append("s.archived = 0")
                if filter == "pinned":
                    where.append("s.pinned = 1")
                elif filter == "normal":
                    where.append("s.pinned = 0")
            if query:
                escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                clauses = ["s.title LIKE ? ESCAPE '\\'", "EXISTS (SELECT 1 FROM chat_messages m WHERE m.session_id = s.id AND m.content LIKE ? ESCAPE '\\')"]
                params.extend([f"%{escaped}%", f"%{escaped}%"])
                if attachment_session_ids:
                    placeholders = ",".join("?" for _ in attachment_session_ids)
                    clauses.append(f"s.id IN ({placeholders})")
                    params.extend(sorted(attachment_session_ids))
                where.append(f"({' OR '.join(clauses)})")
            rows = conn.execute(
                f"SELECT s.* FROM chat_sessions s WHERE {' AND '.join(where)} ORDER BY s.pinned DESC, s.updated_at DESC, s.id",
                params,
            ).fetchall()
            return [self._summary_in(conn, row) for row in rows]

    def update(
        self,
        owner_id: str,
        session_id: str,
        *,
        idempotency_key: str,
        title: str | None = None,
        pinned: bool | None = None,
        archived: bool | None = None,
    ) -> JSON:
        owner_id = _required(owner_id, "owner_id", 200)
        session_id = _required(session_id, "session_id", 200)
        idempotency_key = _required(idempotency_key, "idempotency_key", 200)
        if title is None and pinned is None and archived is None:
            raise ChatError("invalid_request", "Chat Session update is empty")
        if title is not None:
            title = _safe_title(title)
        request_hash = _hash({"title": title, "pinned": pinned, "archived": archived})
        now = self._clock()
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            replay = conn.execute(
                "SELECT request_hash, response_json FROM chat_session_mutations WHERE owner_id = ? AND idempotency_key = ?",
                (owner_id, idempotency_key),
            ).fetchone()
            if replay is not None:
                if str(replay["request_hash"]) != request_hash:
                    raise ChatError("idempotency_conflict", "Chat Session request conflicts with an accepted request")
                return json.loads(str(replay["response_json"]))
            row = self._owned_in(conn, owner_id, session_id)
            before = self._summary_in(conn, row)
            values: list[object] = []
            assignments: list[str] = []
            if title is not None:
                assignments.extend(["title = ?", "title_manual = 1"])
                values.append(title)
            if pinned is not None:
                assignments.append("pinned = ?")
                values.append(int(pinned))
            if archived is not None:
                assignments.append("archived = ?")
                values.append(int(archived))
                if archived:
                    assignments.append("pinned = 0")
            assignments.append("updated_at = ?")
            values.extend([now, session_id])
            conn.execute(f"UPDATE chat_sessions SET {', '.join(assignments)} WHERE id = ?", values)
            self._append_event(
                conn,
                session_id,
                "session.updated",
                f"session:update:{idempotency_key}",
                {"title": title, "pinned": pinned, "archived": archived},
                now,
            )
            response = self._get_in(conn, owner_id, session_id)
            self._record_mutation(conn, owner_id, session_id, "update", idempotency_key, request_hash, before, response, now)
            conn.commit()
            return response

    def delete(self, owner_id: str, session_id: str, *, idempotency_key: str) -> JSON:
        owner_id = _required(owner_id, "owner_id", 200)
        session_id = _required(session_id, "session_id", 200)
        idempotency_key = _required(idempotency_key, "idempotency_key", 200)
        request_hash = _hash({"session_id": session_id})
        now = self._clock()
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            replay = conn.execute(
                "SELECT request_hash, response_json FROM chat_session_mutations WHERE owner_id = ? AND idempotency_key = ?",
                (owner_id, idempotency_key),
            ).fetchone()
            if replay is not None:
                if str(replay["request_hash"]) != request_hash:
                    raise ChatError("idempotency_conflict", "Chat Session request conflicts with an accepted request")
                return json.loads(str(replay["response_json"]))
            row = self._owned_in(conn, owner_id, session_id)
            before = self._summary_in(conn, row)
            response: JSON = {"chat_session_id": session_id, "deleted": True}
            self._record_mutation(conn, owner_id, session_id, "delete", idempotency_key, request_hash, before, response, now)
            conn.execute("DELETE FROM chat_sessions WHERE id = ? AND owner_id = ?", (session_id, owner_id))
            conn.commit()
            ChatAttachments(self._database).collect_garbage()
            return response

    def get(self, owner_id: str, session_id: str) -> JSON:
        owner_id = _required(owner_id, "owner_id", 200)
        session_id = _required(session_id, "session_id", 200)
        with self._database.connect() as conn:
            return self._get_in(conn, owner_id, session_id)

    def send(
        self,
        owner_id: str,
        session_id: str,
        *,
        content: str,
        idempotency_key: str,
        respond: Responder,
        scope: JSON | None = None,
        attachment_ids: list[str] | None = None,
        bind_attachments: Callable[[sqlite3.Connection, str, str, str, list[str]], object] | None = None,
    ) -> JSON:
        owner_id = _required(owner_id, "owner_id", 200)
        session_id = _required(session_id, "session_id", 200)
        raw_content = _required(content, "content", 8_000)
        content = _safe_content(raw_content)
        frozen_scope = _safe_scope(scope)
        idempotency_key = _required(idempotency_key, "idempotency_key", 200)
        normalized_attachment_ids = attachment_ids or []
        if not isinstance(normalized_attachment_ids, list) or any(
            not isinstance(item, str) for item in normalized_attachment_ids
        ):
            raise ChatError("invalid_request", "attachment_ids must be an array of strings")
        request: JSON = {"content": raw_content, "scope": frozen_scope}
        if normalized_attachment_ids:
            request["attachment_ids"] = normalized_attachment_ids
        request_hash = _hash(request)
        now = self._clock()
        duplicate = False
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            session_row = self._owned_in(conn, owner_id, session_id)
            self._branches.assert_idle(conn, session_id, session_row["current_head_id"])
            accepted = conn.execute(
                "SELECT id, request_hash FROM chat_messages WHERE session_id = ? AND idempotency_key = ?",
                (session_id, idempotency_key),
            ).fetchone()
            if accepted is not None:
                if str(accepted["request_hash"]) != request_hash:
                    raise ChatError("idempotency_conflict", "Chat message conflicts with an accepted request")
                duplicate = True
            else:
                position = int(conn.execute(
                    "SELECT COALESCE(MAX(position), 0) FROM chat_messages WHERE session_id = ?", (session_id,)
                ).fetchone()[0])
                user_id, assistant_id = self._id_factory(), self._id_factory()
                conn.execute(
                    """INSERT INTO chat_messages
                       (id, session_id, role, status, position, content, parent_id, idempotency_key, request_hash, scope_json, created_at, updated_at)
                       VALUES (?, ?, 'user', 'completed', ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        user_id, session_id, position + 1, content, session_row["current_head_id"],
                        idempotency_key, request_hash, _canonical(frozen_scope), now, now,
                    ),
                )
                conn.execute(
                    """INSERT INTO chat_messages
                       (id, session_id, role, status, position, content, parent_id, reply_to_id, scope_json, created_at, updated_at)
                       VALUES (?, ?, 'assistant', 'sending', ?, '', ?, ?, ?, ?, ?)""",
                    (assistant_id, session_id, position + 2, user_id, user_id, _canonical(frozen_scope), now, now),
                )
                if normalized_attachment_ids:
                    if bind_attachments is None:
                        raise ChatError("attachment_not_ready", "附件发送绑定未配置")
                    bind_attachments(conn, owner_id, session_id, user_id, normalized_attachment_ids)
                conn.execute(
                    "UPDATE chat_sessions SET title = CASE WHEN title = '新对话' AND title_manual = 0 THEN ? ELSE title END, "
                    "last_scope_json = ?, current_head_id = ?, updated_at = ? WHERE id = ?",
                    (_title(content), _canonical(frozen_scope), assistant_id, now, session_id),
                )
                self._append_event(conn, session_id, "message.created", f"message:{user_id}", {"message_id": user_id}, now)
                self._append_event(conn, session_id, "message.created", f"message:{assistant_id}", {"message_id": assistant_id}, now)
        if duplicate:
            return self.get(owner_id, session_id)
        try:
            result = _chat_result(respond(self._model_request(owner_id, session_id, assistant_id, frozen_scope)), frozen_scope)
        except Exception as exc:
            if isinstance(exc, ChatError) and exc.code == "image_input_unsupported":
                self._finish(owner_id, session_id, assistant_id, status="failed", content=exc.message, error_code=exc.code, result=None)
                raise
            self._finish(owner_id, session_id, assistant_id, status="failed", content="暂时无法回答，请重试。", error_code="model_unavailable", result=None)
        else:
            self._finish(owner_id, session_id, assistant_id, status="completed", content=str(result["answer"]), error_code=None, result=result)
        return self.get(owner_id, session_id)

    def retry(
        self,
        owner_id: str,
        session_id: str,
        message_id: str,
        *,
        respond: Responder,
        refreeze_scope: ScopeRefreezer | None = None,
    ) -> JSON:
        owner_id = _required(owner_id, "owner_id", 200)
        session_id = _required(session_id, "session_id", 200)
        message_id = _required(message_id, "message_id", 200)
        now = self._clock()
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            session_row = self._owned_in(conn, owner_id, session_id)
            self._branches.assert_idle(conn, session_id, session_row["current_head_id"])
            if message_id not in self._branches.ids_in(conn, session_id, session_row["current_head_id"]):
                raise ChatError("message_not_retryable", "Chat message is not retryable")
            row = conn.execute(
                "SELECT scope_json FROM chat_messages WHERE id = ? AND session_id = ? AND role = 'assistant'",
                (message_id, session_id),
            ).fetchone()
            frozen_scope = json.loads(str(row["scope_json"])) if row is not None and row["scope_json"] else None
            if frozen_scope is not None:
                selection = frozen_scope.get("selection") if isinstance(frozen_scope, dict) else None
                if refreeze_scope is None or not isinstance(selection, dict):
                    raise ChatError("chat_scope_not_found", "Chat resource scope not found")
                current_scope = _safe_scope(refreeze_scope(selection))
                if current_scope != frozen_scope:
                    raise ChatError("chat_scope_changed", "Chat resource scope changed; send a new message")
            updated = conn.execute(
                """UPDATE chat_messages SET status = 'sending', content = '', error_code = NULL, scope_json = ?, updated_at = ?
                   WHERE id = ? AND session_id = ? AND role = 'assistant' AND status = 'failed'""",
                (_canonical(frozen_scope), now, message_id, session_id),
            ).rowcount
            if updated != 1:
                raise ChatError("message_not_retryable", "Chat message is not retryable")
            conn.execute(
                "UPDATE chat_sessions SET last_scope_json = ?, current_head_id = ?, updated_at = ? WHERE id = ?",
                (_canonical(frozen_scope), message_id, now, session_id),
            )
            self._append_event(
                conn, session_id, "message.sending", f"message:{message_id}:retry:{self._id_factory()}",
                {"message_id": message_id}, now,
            )
        try:
            result = _chat_result(respond(self._model_request(owner_id, session_id, message_id, frozen_scope)), frozen_scope)
        except Exception as exc:
            if isinstance(exc, ChatError) and exc.code == "image_input_unsupported":
                self._finish(owner_id, session_id, message_id, status="failed", content=exc.message, error_code=exc.code, result=None)
                raise
            self._finish(owner_id, session_id, message_id, status="failed", content="暂时无法回答，请重试。", error_code="model_unavailable", result=None)
        else:
            self._finish(owner_id, session_id, message_id, status="completed", content=str(result["answer"]), error_code=None, result=result)
        return self.get(owner_id, session_id)

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
    ) -> JSON:
        return self._branches.edit(
            owner_id, session_id, message_id,
            content=content, idempotency_key=idempotency_key, respond=respond, scope=scope,
        )

    def reload(
        self,
        owner_id: str,
        session_id: str,
        message_id: str,
        *,
        idempotency_key: str,
        respond: Responder,
    ) -> JSON:
        return self._branches.reload(
            owner_id, session_id, message_id, idempotency_key=idempotency_key, respond=respond,
        )

    def switch_branch(
        self,
        owner_id: str,
        session_id: str,
        message_id: str,
        *,
        idempotency_key: str,
    ) -> JSON:
        return self._branches.switch_branch(
            owner_id, session_id, message_id, idempotency_key=idempotency_key,
        )

    def list_events(self, owner_id: str, session_id: str, *, after: int = 0, limit: int = 200) -> JSON:
        if after < 0 or limit < 1 or limit > 200:
            raise ChatError("invalid_pagination", "Chat event pagination is invalid")
        with self._database.connect() as conn:
            self._owned_in(conn, _required(owner_id, "owner_id", 200), _required(session_id, "session_id", 200))
            rows = conn.execute(
                "SELECT * FROM chat_events WHERE session_id = ? AND event_id > ? ORDER BY event_id LIMIT ?",
                (session_id, after, limit + 1),
            ).fetchall()
            page, has_more = rows[:limit], len(rows) > limit
            events = [_event(row) for row in page]
            return {
                "events": events,
                "next_cursor": int(events[-1]["id"]) if events else after,
                "has_more": has_more,
            }

    def _model_request(self, owner_id: str, session_id: str, head_id: str, scope: JSON | None) -> JSON:
        messages = self._branches.conversation(owner_id, session_id, head_id, include_ids=self._attachment_source is not None)
        attachments: list[JSON] = []
        if self._attachment_source is not None:
            message_ids = [str(item["message_id"]) for item in messages if item.get("message_id")]
            attachments = list(self._attachment_source.model_inputs(owner_id, session_id, message_ids))
        if not attachments:
            messages = [{key: value for key, value in item.items() if key != "message_id"} for item in messages]
        request: JSON = {"request_id": head_id, "messages": messages, "scope": scope}
        request.update({"attachments": attachments} if attachments else {})
        return request

    def _finish(
        self,
        owner_id: str,
        session_id: str,
        message_id: str,
        *,
        status: str,
        content: str,
        error_code: str | None,
        result: JSON | None,
    ) -> None:
        now = self._clock()
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._owned_in(conn, owner_id, session_id)
            updated = conn.execute(
                """UPDATE chat_messages SET status = ?, content = ?, error_code = ?, result_json = ?, updated_at = ?
                   WHERE id = ? AND session_id = ? AND status = 'sending'""",
                (status, content, error_code, _canonical(result), now, message_id, session_id),
            ).rowcount
            if updated != 1:
                raise ChatError("message_conflict", "Chat message is no longer awaiting completion")
            conn.execute("UPDATE chat_sessions SET updated_at = ? WHERE id = ?", (now, session_id))
            self._append_event(
                conn, session_id, f"message.{status}", f"message:{message_id}:{status}:{self._id_factory()}",
                {"message_id": message_id}, now,
            )

    def _owned_in(self, conn: sqlite3.Connection, owner_id: str, session_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM chat_sessions WHERE id = ? AND owner_id = ?",
            (session_id, owner_id),
        ).fetchone()
        if row is None:
            raise ChatError("chat_not_found", "Chat Session not found")
        return row

    def _summary_in(self, conn: sqlite3.Connection, row: sqlite3.Row) -> JSON:
        message_count = conn.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE session_id = ?", (row["id"],)
        ).fetchone()[0]
        return {
            "id": str(row["id"]),
            "title": str(row["title"]),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
            "expires_at": None,
            "message_count": int(message_count),
            "selected_scope": json.loads(str(row["last_scope_json"])) if row["last_scope_json"] else None,
            "pinned": bool(row["pinned"]),
            "archived": bool(row["archived"]),
            "title_manual": bool(row["title_manual"]),
        }

    def _get_in(self, conn: sqlite3.Connection, owner_id: str, session_id: str) -> JSON:
        row = self._owned_in(conn, owner_id, session_id)
        messages = conn.execute(
            "SELECT * FROM chat_messages WHERE session_id = ? ORDER BY position",
            (session_id,),
        ).fetchall()
        current = self._branches.ids_in(conn, session_id, row["current_head_id"])
        groups: dict[tuple[str | None, str], list[str]] = {}
        for message in messages:
            key = (str(message["parent_id"]) if message["parent_id"] is not None else None, str(message["role"]))
            groups.setdefault(key, []).append(str(message["id"]))
        cursor = conn.execute(
            "SELECT COALESCE(MAX(event_id), 0) FROM chat_events WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
        projected = []
        for message in messages:
            key = (str(message["parent_id"]) if message["parent_id"] is not None else None, str(message["role"]))
            siblings = groups[key]
            item = _message(
                message,
                branch_index=siblings.index(str(message["id"])) + 1,
                branch_count=len(siblings),
                is_current_branch=str(message["id"]) in current,
            )
            if self._attachment_source is not None:
                attachment_views = list(self._attachment_source.message_views_in(conn, owner_id, session_id, [str(message["id"])]))
                if attachment_views:
                    item["attachments"] = attachment_views
            projected.append(item)
        return {
            **self._summary_in(conn, row),
            "current_branch_head_id": str(row["current_head_id"]) if row["current_head_id"] is not None else None,
            "messages": projected,
            "event_cursor": int(cursor),
        }

    @staticmethod
    def _record_mutation(
        conn: sqlite3.Connection,
        owner_id: str,
        session_id: str,
        action: str,
        idempotency_key: str,
        request_hash: str,
        before: JSON,
        response: JSON,
        created_at: float,
    ) -> None:
        conn.execute(
            """INSERT INTO chat_session_mutations
               (owner_id, session_id, action, idempotency_key, request_hash, before_json, response_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (owner_id, session_id, action, idempotency_key, request_hash, _canonical(before), _canonical(response), created_at),
        )

    @staticmethod
    def _append_event(
        conn: sqlite3.Connection,
        session_id: str,
        event_type: str,
        idempotency_key: str,
        payload: JSON,
        created_at: float,
    ) -> None:
        conn.execute(
            "INSERT INTO chat_events (session_id, type, idempotency_key, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (session_id, event_type, idempotency_key, json.dumps(payload, sort_keys=True, separators=(",", ":")), created_at),
        )

    @staticmethod
    def _purge_in(conn: sqlite3.Connection, now: float) -> None:
        del conn, now


def _message(
    row: sqlite3.Row,
    *,
    branch_index: int,
    branch_count: int,
    is_current_branch: bool,
) -> JSON:
    scope = json.loads(str(row["scope_json"])) if row["scope_json"] else None
    result = json.loads(str(row["result_json"])) if row["result_json"] else {}
    return {
        "id": str(row["id"]),
        "role": str(row["role"]),
        "status": str(row["status"]),
        "content": str(row["content"]),
        "parent_id": str(row["parent_id"]) if row["parent_id"] is not None else None,
        "reply_to_id": str(row["reply_to_id"]) if row["reply_to_id"] is not None else None,
        "branch_index": branch_index,
        "branch_count": branch_count,
        "is_current_branch": is_current_branch,
        "error_code": str(row["error_code"]) if row["error_code"] is not None else None,
        "mode": result.get("mode") or ("environment" if scope else "knowledge"),
        "scope": scope,
        "tool_activity": list(result.get("tool_activity") or []),
        "evidence_references": list(result.get("evidence_references") or []),
        "uncertainty": result.get("uncertainty"),
        "next_step": result.get("next_step"),
        "completion": result.get("completion"),
        "skill_versions": list(result.get("skill_versions") or []),
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
    }


def _event(row: sqlite3.Row) -> JSON:
    return {
        "id": int(row["event_id"]),
        "session_id": str(row["session_id"]),
        "type": str(row["type"]),
        "payload": json.loads(str(row["payload_json"])),
        "created_at": float(row["created_at"]),
    }


def _required(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise ChatError("invalid_request", f"{field} is required")
    return value.strip()


def _title(content: str) -> str:
    return content[:80]


def _safe_title(title: str) -> str:
    value = _safe_content(title).strip()
    if not value or len(value) > 120:
        raise ChatError("invalid_title", "Chat Session title must be between 1 and 120 characters")
    return value


def _safe_content(content: str) -> str:
    return redact(content)


def _hash(value: JSON) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _canonical(value: object) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_scope(value: JSON | None) -> JSON | None:
    if value is None:
        return None
    encoded = _canonical(value)
    if (
        not isinstance(value.get("revision"), str)
        or len(str(value["revision"])) != 64
        or not isinstance(value.get("resources"), list)
        or not value["resources"]
        or encoded is None
        or len(encoded.encode()) > 64 * 1024
    ):
        raise ChatError("invalid_scope", "Chat resource scope is invalid")
    return json.loads(encoded)


def _chat_result(value: object, scope: JSON | None) -> JSON:
    fields = {
        "mode", "answer", "scope", "tool_activity", "evidence_references",
        "uncertainty", "next_step", "completion", "skill_versions",
    }
    if not isinstance(value, dict) or not fields - {"skill_versions"} <= set(value) <= fields:
        raise ChatError("invalid_model_response", "Chat model result is invalid")
    mode = value.get("mode")
    if mode not in {"knowledge", "environment"} or (mode == "environment") != (scope is not None):
        raise ChatError("invalid_model_response", "Chat model result mode is invalid")
    if value.get("scope") != scope:
        raise ChatError("invalid_model_response", "Chat model result scope changed")
    answer = _safe_content(_required(value.get("answer"), "answer", 16_000))
    tool_activity = value.get("tool_activity")
    references = value.get("evidence_references")
    completion = value.get("completion")
    uncertainty = value.get("uncertainty")
    next_step = value.get("next_step")
    try:
        versions = normalize_skill_versions(value.get("skill_versions"))
    except ValueError as exc:
        raise ChatError("invalid_model_response", str(exc)) from exc
    if (
        not isinstance(tool_activity, list) or len(tool_activity) > 48
        or not isinstance(references, list) or len(references) > 48
        or any(not isinstance(ref, str) or not ref or len(ref) > 500 for ref in references)
        or not isinstance(completion, dict)
        or uncertainty is not None and not isinstance(uncertainty, dict)
        or next_step is not None and (not isinstance(next_step, str) or len(next_step) > 4_000)
    ):
        raise ChatError("invalid_model_response", "Chat model result fields are invalid")
    projected_activity: list[JSON] = []
    scope_fields = {
        "deployment_target_id", "cluster_id", "namespace", "service_id", "service",
        "workload_kind", "workload_name",
    }
    optional_fields = {"missing_reason", "purpose", "source_type", "evidence_step_id", "tool_call_id"}
    for activity in tool_activity:
        if not isinstance(activity, dict) or activity.get("status") not in {
            "succeeded", "partial", "failed", "skipped", "unknown",
        }:
            raise ChatError("invalid_model_response", "Chat Tool Activity is invalid")
        authorized = activity.get("authorized_scope")
        if not isinstance(authorized, dict):
            raise ChatError("invalid_model_response", "Chat Tool Activity scope is invalid")
        projected: JSON = {
            "tool": _safe_content(_required(activity.get("tool"), "tool", 200)),
            "status": activity["status"],
            "summary": _safe_content(_required(activity.get("summary"), "summary", 4_000)),
            "authorized_scope": {
                key: _safe_content(_required(value, key, 500))
                for key, value in authorized.items() if key in scope_fields
            },
        }
        if not projected["authorized_scope"]:
            raise ChatError("invalid_model_response", "Chat Tool Activity scope is invalid")
        for field in optional_fields:
            if field in activity:
                value = activity[field]
                if value is None and field == "missing_reason":
                    projected[field] = None
                elif isinstance(value, str):
                    projected[field] = _safe_content(_required(value, field, 1_000))
                else:
                    raise ChatError("invalid_model_response", "Chat Tool Activity field is invalid")
        try:
            activity_versions = normalize_skill_versions(activity.get("skill_versions"))
        except ValueError as exc:
            raise ChatError("invalid_model_response", str(exc)) from exc
        if activity_versions != versions:
            raise ChatError("invalid_model_response", "Chat Tool Activity Skill versions changed")
        projected["skill_versions"] = activity_versions
        projected_activity.append(projected)
    safe = _safe_value({
        **value,
        "answer": answer,
        "tool_activity": projected_activity,
        "evidence_references": [_safe_content(ref) for ref in references],
        "next_step": _safe_content(next_step) if isinstance(next_step, str) else None,
        "skill_versions": versions,
    })
    assert isinstance(safe, dict)
    safe["scope"] = scope
    encoded = _canonical(safe) or ""
    if len(encoded.encode()) > 128 * 1024:
        raise ChatError("invalid_model_response", "Chat model result is too large")
    return safe


def _safe_value(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else _safe_value(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_safe_value(item) for item in value]
    return _safe_content(value) if isinstance(value, str) else value
