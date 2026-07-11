"""Shared Gateway database constraints."""

from __future__ import annotations

import json
import sqlite3
import time


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
            time.time(),
        ),
    )
