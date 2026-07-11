"""Gateway-owned immutable Investigation Event history and Human Input."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Callable

from .evidence_decisions import invalidate_decisions
from .gateway_db import GatewayDatabase, register_migrations
from .notification_requests import enqueue_investigation_event


JSON = dict[str, object]
_SCHEMA_VERSION = 8
_SCHEMA = """
CREATE TABLE investigation_events (
    investigation_id TEXT NOT NULL,
    event_id INTEGER NOT NULL CHECK (event_id > 0),
    idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) > 0),
    event_type TEXT NOT NULL CHECK (length(event_type) > 0),
    actor_id TEXT,
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (investigation_id, event_id),
    UNIQUE (investigation_id, idempotency_key),
    FOREIGN KEY (investigation_id) REFERENCES investigations(id)
);
CREATE INDEX investigation_events_replay ON investigation_events(investigation_id, event_id);
"""
register_migrations(((_SCHEMA_VERSION, _SCHEMA),))


class InvestigationEventError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def append_event(
    conn: sqlite3.Connection,
    *,
    investigation_id: str,
    event_type: str,
    idempotency_key: str,
    payload: JSON,
    created_at: float,
    actor_id: str | None = None,
) -> JSON:
    """Append once inside the caller's transaction."""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    content_hash = hashlib.sha256(f"{event_type}\0{actor_id or ''}\0{canonical}".encode()).hexdigest()
    existing = conn.execute(
        "SELECT * FROM investigation_events WHERE investigation_id = ? AND idempotency_key = ?",
        (investigation_id, idempotency_key),
    ).fetchone()
    if existing is not None:
        if str(existing["content_hash"]) != content_hash:
            raise InvestigationEventError("idempotency_conflict", "same request key was used for different content")
        return _event(existing)
    event_id = int(
        conn.execute(
            "SELECT COALESCE(MAX(event_id), 0) + 1 FROM investigation_events WHERE investigation_id = ?",
            (investigation_id,),
        ).fetchone()[0]
    )
    conn.execute(
        """
        INSERT INTO investigation_events (
            investigation_id, event_id, idempotency_key, event_type, actor_id,
            payload_json, content_hash, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (investigation_id, event_id, idempotency_key, event_type, actor_id, canonical, content_hash, created_at),
    )
    outcome = str(payload.get("status") or "") if event_type == "diagnosis.output" else ""
    notification_type = {
        "needs_human": "investigation.needs_input",
        "partial": "investigation.partial",
    }.get(outcome)
    if notification_type:
        enqueue_investigation_event(
            conn,
            event_type=notification_type,
            investigation_id=investigation_id,
            now=created_at,
            reason=outcome,
        )
    return {
        "id": event_id,
        "investigation_id": investigation_id,
        "type": event_type,
        "actor_id": actor_id,
        "payload": payload,
        "created_at": created_at,
    }


def transition_investigation(
    conn: sqlite3.Connection,
    *,
    investigation_id: str,
    to_status: str,
    allowed_from: set[str],
    reason: str,
    idempotency_key: str,
    created_at: float,
    actor_id: str | None = None,
) -> bool:
    row = conn.execute("SELECT status FROM investigations WHERE id = ?", (investigation_id,)).fetchone()
    if row is None or str(row["status"]) not in allowed_from:
        return False
    from_status = str(row["status"])
    conn.execute(
        "UPDATE investigations SET status = ?, updated_at = ? WHERE id = ?",
        (to_status, created_at, investigation_id),
    )
    append_event(
        conn,
        investigation_id=investigation_id,
        event_type="investigation.lifecycle",
        idempotency_key=idempotency_key,
        payload={"from": from_status, "to": to_status, "reason": reason},
        actor_id=actor_id,
        created_at=created_at,
    )
    if to_status == "failed":
        enqueue_investigation_event(
            conn,
            event_type="investigation.failed",
            investigation_id=investigation_id,
            now=created_at,
            reason=reason,
        )
    return True


class InvestigationEvents:
    """Replays Investigation Events and appends immutable Human Input."""

    def __init__(
        self,
        database: GatewayDatabase | Path | str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._database = database if isinstance(database, GatewayDatabase) else GatewayDatabase(database)
        self._clock = clock

    def list(self, investigation_id: str, *, after: int = 0, limit: int = 100) -> JSON:
        page, _ = self.poll(investigation_id, after=after, limit=limit)
        return page

    def poll(self, investigation_id: str, *, after: int = 0, limit: int = 100) -> tuple[JSON, str]:
        if after < 0 or not 1 <= limit <= 200:
            raise InvestigationEventError("invalid_pagination", "after and limit are outside the supported range")
        with self._database.connect() as conn:
            conn.execute("BEGIN")
            investigation = conn.execute("SELECT status FROM investigations WHERE id = ?", (investigation_id,)).fetchone()
            if investigation is None:
                raise InvestigationEventError("investigation_not_found", "Investigation was not found")
            rows = conn.execute(
                """
                SELECT * FROM investigation_events
                WHERE investigation_id = ? AND event_id > ?
                ORDER BY event_id LIMIT ?
                """,
                (investigation_id, after, limit + 1),
            ).fetchall()
        events = [_event(row) for row in rows[:limit]]
        return (
            {
                "events": events,
                "next_cursor": int(events[-1]["id"]) if events else after,
                "has_more": len(rows) > limit,
            },
            str(investigation["status"]),
        )

    def submit_human_input(
        self,
        investigation_id: str,
        *,
        kind: str,
        content: str,
        actor_id: str,
        idempotency_key: str,
        target_event_id: int | None = None,
    ) -> JSON:
        if kind not in {"assertion", "correction", "retraction"}:
            raise InvestigationEventError("invalid_human_input", "unsupported Human Input kind")
        content = content.strip()
        if not content or len(content) > 4000 or not idempotency_key.strip():
            raise InvestigationEventError("invalid_human_input", "content and idempotency_key are required")
        if (kind == "assertion") != (target_event_id is None):
            raise InvestigationEventError("invalid_human_input", "correction and retraction must target Human Input")
        payload: JSON = {"content": content}
        if target_event_id is not None:
            payload["target_event_id"] = target_event_id
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            investigation = conn.execute("SELECT status FROM investigations WHERE id = ?", (investigation_id,)).fetchone()
            if investigation is None:
                raise InvestigationEventError("investigation_not_found", "Investigation was not found")
            existing = conn.execute(
                "SELECT 1 FROM investigation_events WHERE investigation_id = ? AND idempotency_key = ?",
                (investigation_id, idempotency_key.strip()),
            ).fetchone()
            if existing is not None:
                return append_event(
                    conn,
                    investigation_id=investigation_id,
                    event_type=f"human_input.{kind}",
                    idempotency_key=idempotency_key.strip(),
                    payload=payload,
                    actor_id=actor_id,
                    created_at=self._clock(),
                )
            if kind == "assertion" and investigation["status"] in {"completed", "failed", "terminated"}:
                raise InvestigationEventError("investigation_terminal", "terminal Investigation does not accept Human Input")
            if target_event_id is not None:
                target = conn.execute(
                    "SELECT event_type FROM investigation_events WHERE investigation_id = ? AND event_id = ?",
                    (investigation_id, target_event_id),
                ).fetchone()
                if target is None or not str(target["event_type"]).startswith("human_input."):
                    raise InvestigationEventError("invalid_reference", "correction and retraction must target Human Input")
            event = append_event(
                conn,
                investigation_id=investigation_id,
                event_type=f"human_input.{kind}",
                idempotency_key=idempotency_key.strip(),
                payload=payload,
                actor_id=actor_id,
                created_at=self._clock(),
            )
            if target_event_id is not None:
                _invalidate_dependencies(conn, investigation_id, target_event_id, int(event["id"]), self._clock())
            conn.commit()
        return event

    def control(
        self,
        investigation_id: str,
        *,
        action: str,
        actor_id: str,
        idempotency_key: str,
    ) -> JSON:
        transitions = {
            "pause": ({"queued", "running"}, "paused"),
            "takeover": ({"queued", "running", "paused"}, "human_led"),
            "terminate": ({"queued", "running", "paused", "human_led"}, "terminated"),
        }
        if action not in transitions or not idempotency_key.strip():
            raise InvestigationEventError("invalid_control", "unsupported Investigation control")
        allowed_from, to_status = transitions[action]
        now = self._clock()
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM investigation_events WHERE investigation_id = ? AND idempotency_key = ?",
                (investigation_id, idempotency_key.strip()),
            ).fetchone()
            if existing is not None:
                event = _event(existing)
                if event["actor_id"] != actor_id or event["payload"].get("reason") != f"human_{action}":  # type: ignore[union-attr]
                    raise InvestigationEventError("idempotency_conflict", "same request key was used for different content")
                return event
            changed = transition_investigation(
                conn,
                investigation_id=investigation_id,
                to_status=to_status,
                allowed_from=allowed_from,
                reason=f"human_{action}",
                idempotency_key=idempotency_key.strip(),
                actor_id=actor_id,
                created_at=now,
            )
            if not changed:
                raise InvestigationEventError("invalid_transition", f"{action} is not allowed in the current state")
            row = conn.execute(
                "SELECT * FROM investigation_events WHERE investigation_id = ? AND idempotency_key = ?",
                (investigation_id, idempotency_key.strip()),
            ).fetchone()
            conn.commit()
        assert row is not None
        return _event(row)

    def incident_id(self, investigation_id: str) -> str | None:
        with self._database.connect() as conn:
            row = conn.execute("SELECT incident_id FROM investigations WHERE id = ?", (investigation_id,)).fetchone()
        return str(row["incident_id"]) if row is not None else None

def _event(row: sqlite3.Row) -> JSON:
    return {
        "id": int(row["event_id"]),
        "investigation_id": str(row["investigation_id"]),
        "type": str(row["event_type"]),
        "actor_id": row["actor_id"],
        "payload": json.loads(str(row["payload_json"])),
        "created_at": float(row["created_at"]),
    }


def _invalidate_dependencies(
    conn: sqlite3.Connection,
    investigation_id: str,
    target_event_id: int,
    trigger_event_id: int,
    created_at: float,
) -> None:
    affected: set[int] = set()
    current: int | None = target_event_id
    while current is not None and current not in affected:
        affected.add(current)
        row = conn.execute(
            "SELECT payload_json FROM investigation_events WHERE investigation_id = ? AND event_id = ?",
            (investigation_id, current),
        ).fetchone()
        if row is None:
            break
        parent = json.loads(str(row["payload_json"])).get("target_event_id")
        current = parent if isinstance(parent, int) else None

    outputs = conn.execute(
        "SELECT event_id, payload_json FROM investigation_events WHERE investigation_id = ? AND event_type = 'diagnosis.output'",
        (investigation_id,),
    ).fetchall()
    for output in outputs:
        diagnosis = json.loads(str(output["payload_json"])).get("diagnosis", {})
        if not isinstance(diagnosis, dict):
            continue
        dependencies = diagnosis.get("human_input_event_ids", [])
        if not isinstance(dependencies, list) or not affected.intersection(item for item in dependencies if isinstance(item, int)):
            continue
        output_id = int(output["event_id"])
        append_event(
            conn,
            investigation_id=investigation_id,
            event_type="judgment.invalidated",
            idempotency_key=f"judgment-invalidated:{output_id}:{trigger_event_id}",
            payload={"diagnosis_event_id": output_id, "triggered_by_event_id": trigger_event_id},
            created_at=created_at,
        )
        action_ids = diagnosis.get("recommended_action_ids", [])
        if isinstance(action_ids, list):
            canonical_action_ids = [action_id for action_id in action_ids if isinstance(action_id, str) and action_id]
            invalidate_decisions(conn, investigation_id, canonical_action_ids)
            for action_id in action_ids:
                if isinstance(action_id, str) and action_id:
                    append_event(
                        conn,
                        investigation_id=investigation_id,
                        event_type="recommended_action.stale",
                        idempotency_key=f"action-stale:{action_id}:{trigger_event_id}",
                        payload={"recommended_action_id": action_id, "triggered_by_event_id": trigger_event_id},
                        created_at=created_at,
                    )
