"""Gateway-owned private Chat Sessions and durable message events."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Callable

from .gateway_db import GatewayDatabase, register_migrations


JSON = dict[str, object]
Responder = Callable[[JSON], JSON]
ScopeRefreezer = Callable[[JSON], JSON]
_RETENTION_SECONDS = 30 * 24 * 60 * 60
_SECURE_INPUT = re.compile(r"\{\{secure-input:[^}]+\}\}", re.IGNORECASE)
_SECRET = re.compile(
    r"(?i)\b(?:(?:authorization|credential|password|secret|token|api[_-]?key)\s*[:=]\s*(?:bearer\s+)?[^\s,;]+|bearer\s+[^\s,;]+)"
)
_SENSITIVE_KEY = re.compile(r"authorization|credential|password|secret|token|api[_-]?key|secure[_-]?input|raw[_-]?payload", re.IGNORECASE)
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
register_migrations(((_SCHEMA_VERSION, _SCHEMA), (_RESULT_SCHEMA_VERSION, _RESULT_SCHEMA)))


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
    ) -> None:
        self._database = database if isinstance(database, GatewayDatabase) else GatewayDatabase(database)
        self._clock = clock
        self._id_factory = id_factory

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

    def list(self, owner_id: str) -> list[JSON]:
        owner_id = _required(owner_id, "owner_id", 200)
        with self._database.connect() as conn:
            self._purge_in(conn, self._clock())
            rows = conn.execute(
                "SELECT * FROM chat_sessions WHERE owner_id = ? ORDER BY updated_at DESC, id",
                (owner_id,),
            ).fetchall()
            return [self._summary_in(conn, row) for row in rows]

    def get(self, owner_id: str, session_id: str) -> JSON:
        owner_id = _required(owner_id, "owner_id", 200)
        session_id = _required(session_id, "session_id", 200)
        with self._database.connect() as conn:
            self._purge_in(conn, self._clock())
            row = self._owned_in(conn, owner_id, session_id)
            messages = conn.execute(
                "SELECT * FROM chat_messages WHERE session_id = ? ORDER BY position",
                (session_id,),
            ).fetchall()
            cursor = conn.execute(
                "SELECT COALESCE(MAX(event_id), 0) FROM chat_events WHERE session_id = ?", (session_id,)
            ).fetchone()[0]
            return {**self._summary_in(conn, row), "messages": [_message(item) for item in messages], "event_cursor": int(cursor)}

    def send(
        self,
        owner_id: str,
        session_id: str,
        *,
        content: str,
        idempotency_key: str,
        respond: Responder,
        scope: JSON | None = None,
    ) -> JSON:
        owner_id = _required(owner_id, "owner_id", 200)
        session_id = _required(session_id, "session_id", 200)
        raw_content = _required(content, "content", 8_000)
        content = _safe_content(raw_content)
        frozen_scope = _safe_scope(scope)
        idempotency_key = _required(idempotency_key, "idempotency_key", 200)
        request_hash = _hash({"content": raw_content, "scope": frozen_scope})
        now = self._clock()
        duplicate = False
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._purge_in(conn, now)
            self._owned_in(conn, owner_id, session_id)
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
                       (id, session_id, role, status, position, content, idempotency_key, request_hash, scope_json, created_at, updated_at)
                       VALUES (?, ?, 'user', 'completed', ?, ?, ?, ?, ?, ?, ?)""",
                    (user_id, session_id, position + 1, content, idempotency_key, request_hash, _canonical(frozen_scope), now, now),
                )
                conn.execute(
                    """INSERT INTO chat_messages
                       (id, session_id, role, status, position, content, reply_to_id, scope_json, created_at, updated_at)
                       VALUES (?, ?, 'assistant', 'sending', ?, '', ?, ?, ?, ?)""",
                    (assistant_id, session_id, position + 2, user_id, _canonical(frozen_scope), now, now),
                )
                conn.execute(
                    "UPDATE chat_sessions SET title = CASE WHEN title = '新对话' THEN ? ELSE title END, "
                    "last_scope_json = ?, updated_at = ? WHERE id = ?",
                    (_title(content), _canonical(frozen_scope), now, session_id),
                )
                self._append_event(conn, session_id, "message.created", f"message:{user_id}", {"message_id": user_id}, now)
                self._append_event(conn, session_id, "message.created", f"message:{assistant_id}", {"message_id": assistant_id}, now)
        if duplicate:
            return self.get(owner_id, session_id)
        try:
            result = _chat_result(respond({
                "request_id": assistant_id,
                "messages": self._conversation(owner_id, session_id),
                "scope": frozen_scope,
            }), frozen_scope)
        except Exception:
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
            self._purge_in(conn, now)
            self._owned_in(conn, owner_id, session_id)
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
                "UPDATE chat_sessions SET last_scope_json = ?, updated_at = ? WHERE id = ?",
                (_canonical(frozen_scope), now, session_id),
            )
            self._append_event(
                conn, session_id, "message.sending", f"message:{message_id}:retry:{self._id_factory()}",
                {"message_id": message_id}, now,
            )
        try:
            result = _chat_result(respond({
                "request_id": message_id,
                "messages": self._conversation(owner_id, session_id),
                "scope": frozen_scope,
            }), frozen_scope)
        except Exception:
            self._finish(owner_id, session_id, message_id, status="failed", content="暂时无法回答，请重试。", error_code="model_unavailable", result=None)
        else:
            self._finish(owner_id, session_id, message_id, status="completed", content=str(result["answer"]), error_code=None, result=result)
        return self.get(owner_id, session_id)

    def list_events(self, owner_id: str, session_id: str, *, after: int = 0, limit: int = 200) -> JSON:
        if after < 0 or limit < 1 or limit > 200:
            raise ChatError("invalid_pagination", "Chat event pagination is invalid")
        with self._database.connect() as conn:
            self._purge_in(conn, self._clock())
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

    def _conversation(self, owner_id: str, session_id: str) -> list[dict[str, str]]:
        session = self.get(owner_id, session_id)
        return [
            {"role": str(item["role"]), "content": str(item["content"])}
            for item in session["messages"]  # type: ignore[union-attr]
            if isinstance(item, dict) and item.get("status") == "completed"
        ]

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
            "SELECT * FROM chat_sessions WHERE id = ? AND owner_id = ? AND expires_at > ?",
            (session_id, owner_id, self._clock()),
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
            "expires_at": float(row["expires_at"]),
            "message_count": int(message_count),
            "selected_scope": json.loads(str(row["last_scope_json"])) if row["last_scope_json"] else None,
        }

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
        conn.execute("DELETE FROM chat_sessions WHERE expires_at <= ?", (now,))


def _message(row: sqlite3.Row) -> JSON:
    scope = json.loads(str(row["scope_json"])) if row["scope_json"] else None
    result = json.loads(str(row["result_json"])) if row["result_json"] else {}
    return {
        "id": str(row["id"]),
        "role": str(row["role"]),
        "status": str(row["status"]),
        "content": str(row["content"]),
        "reply_to_id": str(row["reply_to_id"]) if row["reply_to_id"] is not None else None,
        "error_code": str(row["error_code"]) if row["error_code"] is not None else None,
        "mode": result.get("mode") or ("environment" if scope else "knowledge"),
        "scope": scope,
        "tool_activity": list(result.get("tool_activity") or []),
        "evidence_references": list(result.get("evidence_references") or []),
        "uncertainty": result.get("uncertainty"),
        "next_step": result.get("next_step"),
        "completion": result.get("completion"),
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


def _safe_content(content: str) -> str:
    return _SECRET.sub("[REDACTED]", _SECURE_INPUT.sub("[REDACTED]", content))


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
        "uncertainty", "next_step", "completion",
    }
    if not isinstance(value, dict) or set(value) != fields:
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
        projected_activity.append(projected)
    safe = _safe_value({
        **value,
        "answer": answer,
        "tool_activity": projected_activity,
        "evidence_references": [_safe_content(ref) for ref in references],
        "next_step": _safe_content(next_step) if isinstance(next_step, str) else None,
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
