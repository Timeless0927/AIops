"""Change Plan Phase state and immutable transition events."""

from __future__ import annotations

import json
import sqlite3

from .change_requests import ChangeRequestError


class ChangePlanPhases:
    @staticmethod
    def approval_context_in(
        conn: sqlite3.Connection, change_request_id: str,
    ) -> dict[str, object] | None:
        row = conn.execute(
            """
            SELECT cr.id AS change_request_id, phase.id AS phase_id,
                   COALESCE(phase.execution_status, phase.approval_status, phase.status) AS phase_status,
                   revision.id AS revision_id, revision.revision AS revision_number,
                   revision.plan_json
            FROM change_requests cr
            JOIN change_plan_phases phase ON phase.change_request_id = cr.id
            JOIN change_plan_revisions revision ON revision.phase_id = phase.id
            WHERE cr.id = ? AND revision.status != 'superseded'
            ORDER BY phase.sequence DESC, revision.revision DESC LIMIT 1
            """,
            (change_request_id,),
        ).fetchone()
        if row is None:
            return None
        plan = json.loads(str(row["plan_json"])) if row["plan_json"] else None
        return {**dict(row), "plan": plan}

    @staticmethod
    def approval_context_for_phase_in(
        conn: sqlite3.Connection,
        phase_id: str,
        revision_id: str,
    ) -> dict[str, object] | None:
        row = conn.execute(
            """
            SELECT cr.id AS change_request_id, phase.id AS phase_id,
                   COALESCE(phase.execution_status, phase.approval_status, phase.status) AS phase_status,
                   revision.id AS revision_id, revision.revision AS revision_number,
                   revision.plan_json
            FROM change_requests cr
            JOIN change_plan_phases phase ON phase.change_request_id = cr.id
            JOIN change_plan_revisions revision ON revision.phase_id = phase.id
            WHERE phase.id = ? AND revision.id = ? AND revision.status != 'superseded'
            """,
            (phase_id, revision_id),
        ).fetchone()
        if row is None:
            return None
        plan = json.loads(str(row["plan_json"])) if row["plan_json"] else None
        return {**dict(row), "plan": plan}

    @staticmethod
    def change_request_id_in(conn: sqlite3.Connection, phase_id: str) -> str | None:
        row = conn.execute(
            "SELECT change_request_id FROM change_plan_phases WHERE id = ?", (phase_id,),
        ).fetchone()
        return str(row["change_request_id"]) if row is not None else None

    @staticmethod
    def record_approved_in(
        conn: sqlite3.Connection,
        *,
        change_request_id: str,
        phase_id: str,
        revision_id: str,
        approval_id: str,
        actor_id: str,
        rollback_policy: str,
        start_expires_at: float,
        request_id: str,
        now: float,
    ) -> None:
        updated = conn.execute(
            """
            UPDATE change_plan_phases SET approval_status = 'approved', updated_at = ?
            WHERE id = ? AND status = 'awaiting_approval' AND approval_status IS NULL
            """,
            (now, phase_id),
        )
        if updated.rowcount != 1:
            raise ChangeRequestError("phase_stale", "Change Plan Phase is no longer approvable")
        conn.execute("UPDATE change_requests SET updated_at = ? WHERE id = ?", (now, change_request_id))
        _append_event(
            conn, change_request_id, "change_request.phase_approved", actor_id,
            {
                "phase_id": phase_id, "revision_id": revision_id, "approval_id": approval_id,
                "rollback_policy": rollback_policy, "start_expires_at": start_expires_at,
                "request_id": request_id,
            },
            now,
        )

    @staticmethod
    def record_expired_in(
        conn: sqlite3.Connection,
        *,
        change_request_id: str,
        phase_id: str,
        revision_id: str,
        reason: str,
        now: float,
    ) -> None:
        updated = conn.execute(
            """
            UPDATE change_plan_phases SET approval_status = 'expired', updated_at = ?
            WHERE id = ? AND COALESCE(approval_status, status) != 'expired'
            """,
            (now, phase_id),
        )
        if updated.rowcount != 1:
            return
        conn.execute("UPDATE change_requests SET updated_at = ? WHERE id = ?", (now, change_request_id))
        _append_event(
            conn, change_request_id, "change_request.phase_expired", None,
            {"phase_id": phase_id, "revision_id": revision_id, "reason": reason}, now,
        )

    @staticmethod
    def record_execution_queued_in(
        conn: sqlite3.Connection,
        *,
        change_request_id: str,
        phase_id: str,
        execution_id: str,
        grant_id: str,
        actor_id: str,
        request_id: str,
        now: float,
    ) -> None:
        conn.execute("UPDATE change_requests SET updated_at = ? WHERE id = ?", (now, change_request_id))
        _append_event(
            conn, change_request_id, "change_request.execution_queued", actor_id,
            {
                "phase_id": phase_id, "execution_id": execution_id,
                "grant_id": grant_id, "request_id": request_id,
            },
            now,
        )

    @staticmethod
    def record_execution_started_in(
        conn: sqlite3.Connection,
        *,
        change_request_id: str,
        phase_id: str,
        execution_id: str,
        command_id: str,
        now: float,
    ) -> None:
        updated = conn.execute(
            "UPDATE change_plan_phases SET execution_status = 'executing', updated_at = ? "
            "WHERE id = ? AND approval_status = 'approved' AND execution_status IS NULL",
            (now, phase_id),
        )
        if updated.rowcount != 1:
            raise ChangeRequestError("phase_stale", "Approved Phase is no longer startable")
        conn.execute("UPDATE change_requests SET updated_at = ? WHERE id = ?", (now, change_request_id))
        _append_event(
            conn, change_request_id, "change_request.execution_started", None,
            {"phase_id": phase_id, "execution_id": execution_id, "command_id": command_id}, now,
        )

    @staticmethod
    def record_execution_finished_in(
        conn: sqlite3.Connection,
        *,
        change_request_id: str,
        phase_id: str,
        execution_id: str,
        command_id: str,
        outcome: str,
        error_code: str | None,
        now: float,
    ) -> None:
        phase_status = (
            "succeeded" if outcome == "succeeded"
            else "unknown_outcome" if outcome == "unknown_outcome"
            else "failed"
        )
        conn.execute(
            "UPDATE change_plan_phases SET execution_status = ?, updated_at = ? WHERE id = ?",
            (phase_status, now, phase_id),
        )
        conn.execute("UPDATE change_requests SET updated_at = ? WHERE id = ?", (now, change_request_id))
        _append_event(
            conn, change_request_id,
            "change_request.execution_outcome_unknown"
            if outcome == "unknown_outcome" else "change_request.execution_finished",
            None,
            {
                "phase_id": phase_id, "execution_id": execution_id, "command_id": command_id,
                "outcome": outcome, "error_code": error_code,
            },
            now,
        )


def _append_event(
    conn: sqlite3.Connection,
    change_request_id: str,
    event_type: str,
    actor_id: str | None,
    payload: dict[str, object],
    now: float,
) -> None:
    conn.execute(
        """
        INSERT INTO change_request_events (
            change_request_id, type, actor_id, payload_json, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (change_request_id, event_type, actor_id, json.dumps(payload, sort_keys=True), now),
    )
