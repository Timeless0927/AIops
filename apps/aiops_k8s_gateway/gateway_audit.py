"""Gateway administration audit records."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable

from .gateway_db import GatewayDatabase


def insert_admin_audit(
    conn: sqlite3.Connection,
    *,
    actor_id: str | None,
    target_type: str,
    target_id: str | None,
    action: str,
    reason: str,
    before: dict[str, object] | None,
    after: dict[str, object] | None,
    result: str,
    request_id: str,
    clock: Callable[[], float] = time.time,
) -> None:
    conn.execute(
        """
        INSERT INTO admin_audit (
            actor_id, target_type, target_id, action, reason,
            before_json, after_json, result, request_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            actor_id,
            target_type,
            target_id,
            action,
            reason,
            json.dumps(before, ensure_ascii=False, sort_keys=True) if before else None,
            json.dumps(after, ensure_ascii=False, sort_keys=True) if after else None,
            result,
            request_id,
            clock(),
        ),
    )


class GatewayAudit:
    def __init__(self, database: GatewayDatabase, *, clock: Callable[[], float] = time.time) -> None:
        self._database = database
        self._clock = clock

    def insert_in(
        self,
        conn: sqlite3.Connection,
        *,
        actor_id: str | None,
        target_type: str,
        target_id: str | None,
        action: str,
        reason: str,
        before: dict[str, object] | None,
        after: dict[str, object] | None,
        result: str,
        request_id: str,
    ) -> None:
        insert_admin_audit(
            conn,
            actor_id=actor_id,
            target_type=target_type,
            target_id=target_id,
            action=action,
            reason=reason,
            before=before,
            after=after,
            result=result,
            request_id=request_id,
            clock=self._clock,
        )

    def record(
        self,
        *,
        actor_id: str | None,
        target_type: str,
        target_id: str | None,
        action: str,
        reason: str,
        before: dict[str, object] | None,
        after: dict[str, object] | None,
        result: str,
        request_id: str,
    ) -> None:
        with self._database.connect() as conn:
            insert_admin_audit(
                conn,
                actor_id=actor_id,
                target_type=target_type,
                target_id=target_id,
                action=action,
                reason=reason,
                before=before,
                after=after,
                result=result,
                request_id=request_id,
                clock=self._clock,
            )

    def denial(
        self,
        actor_id: str | None,
        audit_target: tuple[str, str | None, str] | None,
        result: str,
        request_id: str,
        reason: str = "unavailable_before_authorization",
    ) -> None:
        if audit_target is None:
            return
        target_type, target_id, action = audit_target
        self.record(
            actor_id=actor_id,
            target_type=target_type,
            target_id=target_id,
            action=action,
            reason=reason,
            before=None,
            after=None,
            result=result,
            request_id=request_id,
        )

    def recent(self) -> list[dict[str, object]]:
        with self._database.connect() as conn:
            rows = conn.execute("SELECT * FROM admin_audit ORDER BY created_at DESC LIMIT 100").fetchall()
        return [
            {
                **{key: value for key, value in dict(row).items() if key not in {"before_json", "after_json"}},
                "before": json.loads(str(row["before_json"])) if row["before_json"] else None,
                "after": json.loads(str(row["after_json"])) if row["after_json"] else None,
            }
            for row in rows
        ]

    def unresolved_request(
        self,
        target_type: str,
        target_id: str | None,
        action: str,
    ) -> str | None:
        with self._database.connect() as conn:
            row = conn.execute(
                """SELECT pending.request_id FROM admin_audit pending
                   WHERE pending.target_type = ? AND pending.target_id IS ?
                     AND pending.action = ? AND pending.result = 'outcome_unknown'
                     AND NOT EXISTS (
                       SELECT 1 FROM admin_audit later
                       WHERE later.request_id = pending.request_id
                         AND (later.created_at > pending.created_at OR
                              (later.created_at = pending.created_at AND later.rowid > pending.rowid))
                     )
                   ORDER BY pending.created_at DESC, pending.rowid DESC LIMIT 1""",
                (target_type, target_id, action),
            ).fetchone()
        return str(row["request_id"]) if row is not None else None
