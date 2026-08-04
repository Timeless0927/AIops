"""Gateway-owned explicit transfer from private Chat to governed Investigation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Callable

from .gateway_db import GatewayDatabase, register_migrations
from .incident import IncidentError, create_user_incident_in
from .investigation_events import append_event


JSON = dict[str, object]
_SCHEMA_VERSION = 48
_SCHEMA = """
CREATE TABLE chat_handoffs (
    id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    chat_session_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
    target_type TEXT NOT NULL CHECK (target_type IN ('existing_incident', 'user_created_incident')),
    incident_id TEXT NOT NULL REFERENCES incidents(id),
    investigation_id TEXT NOT NULL REFERENCES investigations(id),
    selected_message_ids_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(actor_id, idempotency_key)
);
CREATE INDEX chat_handoffs_incident ON chat_handoffs(incident_id, created_at, id);
"""
register_migrations(((_SCHEMA_VERSION, _SCHEMA),))


class ChatHandoffError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ChatHandoffs:
    """Commit selected Chat context as non-evidence Human Input exactly once."""

    def __init__(
        self,
        database: GatewayDatabase | Path | str,
        *,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[str], str] = lambda prefix: f"{prefix}-{uuid.uuid4().hex}",
    ) -> None:
        self._database = database if isinstance(database, GatewayDatabase) else GatewayDatabase(database)
        self._clock = clock
        self._id_factory = id_factory

    def execute(
        self,
        *,
        actor_id: str,
        session_id: str,
        message_ids: list[str],
        idempotency_key: str,
        team_ids: set[str] | None,
        target_incident_id: str | None = None,
        problem_summary: str | None = None,
        frozen_scope: JSON | None = None,
    ) -> JSON:
        actor_id = _text(actor_id, "actor_id", 200)
        session_id = _text(session_id, "session_id", 200)
        idempotency_key = _text(idempotency_key, "idempotency_key", 200)
        selected = _message_ids(message_ids)
        if target_incident_id is not None and (problem_summary is not None or frozen_scope is not None):
            raise ChatHandoffError("invalid_handoff", "Handoff target is ambiguous")
        if target_incident_id is not None:
            target_incident_id = _text(target_incident_id, "target_incident_id", 200)
            target_type = "existing_incident"
            target: object = target_incident_id
        else:
            problem_summary = _text(problem_summary, "problem_summary", 2_000)
            frozen_scope = _scope(frozen_scope)
            target_type = "user_created_incident"
            target = {"problem_summary": problem_summary, "scope": frozen_scope["selection"]}
        request_hash = _hash({
            "chat_session_id": session_id,
            "message_ids": sorted(selected),
            "target_type": target_type,
            "target": target,
        })
        now = self._clock()
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            replay = conn.execute(
                "SELECT * FROM chat_handoffs WHERE actor_id = ? AND idempotency_key = ?",
                (actor_id, idempotency_key),
            ).fetchone()
            if replay is not None:
                if str(replay["request_hash"]) != request_hash:
                    raise ChatHandoffError("idempotency_conflict", "Handoff key was used for different content")
                if not _incident_visible(conn, str(replay["incident_id"]), team_ids):
                    raise ChatHandoffError("handoff_target_not_found", "Handoff target not found")
                return _handoff(replay, idempotent=True)
            messages = _selected_messages(conn, actor_id, session_id, selected, now)
            handoff_id = self._id_factory("handoff")
            if target_type == "existing_incident":
                assert target_incident_id is not None
                investigation_id = _active_investigation(conn, target_incident_id, team_ids)
            else:
                assert problem_summary is not None and frozen_scope is not None
                [resource] = frozen_scope["resources"]  # type: ignore[misc]
                assert isinstance(resource, dict)
                try:
                    target_incident_id, investigation_id = create_user_incident_in(
                        conn, problem_summary=problem_summary, resource=resource,
                        team_ids=team_ids, now=now, id_factory=self._id_factory,
                    )
                except IncidentError as exc:
                    raise ChatHandoffError(exc.code, exc.message) from exc
            conn.execute(
                """INSERT INTO chat_handoffs (
                       id, actor_id, chat_session_id, idempotency_key, request_hash, target_type,
                       incident_id, investigation_id, selected_message_ids_json, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    handoff_id, actor_id, session_id, idempotency_key, request_hash,
                    target_type, target_incident_id, investigation_id,
                    _canonical([message["id"] for message in messages]), now,
                ),
            )
            append_event(
                conn,
                investigation_id=investigation_id,
                event_type="handoff.created",
                idempotency_key=f"handoff:{handoff_id}",
                payload={
                    "handoff_id": handoff_id, "chat_session_id": session_id,
                    "target_type": target_type,
                    "selected_message_ids": [message["id"] for message in messages],
                },
                actor_id=actor_id,
                created_at=now,
            )
            for message in messages:
                append_event(
                    conn,
                    investigation_id=investigation_id,
                    event_type="human_input.assertion",
                    idempotency_key=f"handoff:{handoff_id}:message:{message['id']}",
                    payload={
                        "content": message["content"], "source": "chat_handoff",
                        "handoff_id": handoff_id, "chat_session_id": session_id,
                        "chat_message_id": message["id"], "chat_role": message["role"],
                    },
                    actor_id=actor_id,
                    created_at=now,
                )
            conn.execute(
                "INSERT INTO chat_events (session_id, type, idempotency_key, payload_json, created_at) VALUES (?, 'handoff.completed', ?, ?, ?)",
                (
                    session_id, f"handoff:{handoff_id}",
                    _canonical({"handoff_id": handoff_id, "incident_id": target_incident_id, "investigation_id": investigation_id}),
                    now,
                ),
            )
            conn.execute("UPDATE chat_sessions SET updated_at = ? WHERE id = ?", (now, session_id))
            row = conn.execute("SELECT * FROM chat_handoffs WHERE id = ?", (handoff_id,)).fetchone()
            conn.commit()
        assert row is not None
        return _handoff(row, idempotent=False)


def _selected_messages(
    conn: sqlite3.Connection,
    actor_id: str,
    session_id: str,
    message_ids: list[str],
    now: float,
) -> list[sqlite3.Row]:
    session = conn.execute(
        "SELECT 1 FROM chat_sessions WHERE id = ? AND owner_id = ? AND expires_at > ?",
        (session_id, actor_id, now),
    ).fetchone()
    if session is None:
        raise ChatHandoffError("chat_not_found", "Chat Session not found")
    placeholders = ",".join("?" for _ in message_ids)
    rows = conn.execute(
        f"SELECT id, role, status, content FROM chat_messages WHERE session_id = ? AND id IN ({placeholders}) ORDER BY position",
        (session_id, *message_ids),
    ).fetchall()
    if len(rows) != len(message_ids) or any(row["status"] != "completed" or not str(row["content"]).strip() for row in rows):
        raise ChatHandoffError("chat_message_not_found", "Selected Chat message not found")
    if sum(len(str(row["content"]).encode()) for row in rows) > 32 * 1024:
        raise ChatHandoffError("handoff_too_large", "Selected Chat context is too large")
    return rows


def _active_investigation(conn: sqlite3.Connection, incident_id: str, team_ids: set[str] | None) -> str:
    if not _incident_visible(conn, incident_id, team_ids):
        raise ChatHandoffError("handoff_target_not_found", "Handoff target not found")
    row = conn.execute(
        "SELECT id, status FROM investigations WHERE incident_id = ? ORDER BY sequence DESC LIMIT 1",
        (incident_id,),
    ).fetchone()
    if row is None:
        raise ChatHandoffError("handoff_target_not_found", "Handoff target not found")
    if row["status"] not in {"queued", "running", "paused", "human_led"}:
        raise ChatHandoffError("investigation_terminal", "Target Investigation does not accept Human Input")
    return str(row["id"])


def _incident_visible(conn: sqlite3.Connection, incident_id: str, team_ids: set[str] | None) -> bool:
    params: list[object] = [incident_id]
    scope = ""
    if team_ids is not None:
        if not team_ids:
            scope = "AND COALESCE(binding.team_id, incident.team_id) IS NULL"
        else:
            placeholders = ",".join("?" for _ in team_ids)
            scope = f"AND (COALESCE(binding.team_id, incident.team_id) IS NULL OR COALESCE(binding.team_id, incident.team_id) IN ({placeholders}))"
            params.extend(sorted(team_ids))
    return conn.execute(
        f"""SELECT 1 FROM incidents incident
            LEFT JOIN resource_bindings binding ON binding.id = incident.resource_binding_id
            WHERE incident.id = ? {scope}""",
        params,
    ).fetchone() is not None


def _message_ids(values: object) -> list[str]:
    if not isinstance(values, list) or not 1 <= len(values) <= 20:
        raise ChatHandoffError("invalid_handoff", "Select between 1 and 20 Chat messages")
    selected = [_text(value, "message_id", 200) for value in values]
    if len(set(selected)) != len(selected):
        raise ChatHandoffError("invalid_handoff", "Selected Chat messages must be unique")
    return selected


def _scope(value: object) -> JSON:
    if not isinstance(value, dict) or set(value) != {"selection", "resources", "time_range", "revision"}:
        raise ChatHandoffError("invalid_handoff", "A frozen resource scope is required")
    resources = value.get("resources")
    if not isinstance(resources, list) or len(resources) != 1 or not isinstance(resources[0], dict):
        raise ChatHandoffError("invalid_handoff", "User-created Incident requires one Deployment Target")
    required = {
        "deployment_target_id", "cluster_id", "namespace", "service_id", "service_name",
        "workload_kind", "workload_name",
    }
    if set(resources[0]) != required or any(not isinstance(resources[0][field], str) or not resources[0][field] for field in required):
        raise ChatHandoffError("invalid_handoff", "User-created Incident resource scope is invalid")
    selection = value.get("selection")
    if not isinstance(selection, dict) or not selection:
        raise ChatHandoffError("invalid_handoff", "User-created Incident resource selection is invalid")
    return json.loads(_canonical(value))


def _text(value: object, field: str, limit: int) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized or len(normalized) > limit:
        raise ChatHandoffError("invalid_handoff", f"{field} is required")
    return normalized


def _hash(value: JSON) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _handoff(row: sqlite3.Row, *, idempotent: bool) -> JSON:
    return {
        "id": str(row["id"]),
        "chat_session_id": str(row["chat_session_id"]),
        "target_type": str(row["target_type"]),
        "incident_id": str(row["incident_id"]),
        "investigation_id": str(row["investigation_id"]),
        "selected_message_ids": json.loads(str(row["selected_message_ids_json"])),
        "created_at": float(row["created_at"]),
        "idempotent": idempotent,
    }
