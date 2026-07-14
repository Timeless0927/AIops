"""Notification Destination revision and readiness projections."""

from __future__ import annotations

import sqlite3


JSON = dict[str, object]
PILOT_ROUTE_ID = "route:pilot-catch-all"


class NotificationDestinationReadiness:
    def decorate(
        self,
        conn: sqlite3.Connection,
        destination: JSON,
        row: sqlite3.Row,
    ) -> JSON:
        revision = str(row["revision"])
        verification = self._verification(conn, str(row["id"]), revision)
        availability = self._availability(conn, str(row["id"]), revision, verification)
        if (
            verification["state"] == "verified"
            and availability["reason_code"] in {"authentication_failed", "provider_rejected"}
        ):
            verification = {
                **verification,
                "state": "failed",
                "checked_at": availability["observed_at"],
                "reason_code": availability["reason_code"],
            }
        selected = self._pilot_route_selected(conn, str(row["id"]))
        ready = verification["state"] == "verified" and availability["state"] == "available" and selected
        return {
            **destination,
            "configuration_revision": revision,
            "readiness": "ready" if ready else "not_ready",
            "verification": verification,
            "availability": availability,
            "pilot_route_selected": selected,
        }

    def record_configuration_changed(
        self,
        conn: sqlite3.Connection,
        destination_id: str,
        revision: str,
        now: float,
    ) -> None:
        conn.execute(
            """INSERT INTO notification_destination_availability VALUES (?, ?, 'unavailable', ?, 'configuration_changed')
               ON CONFLICT(destination_id) DO UPDATE SET
                 revision = excluded.revision, state = excluded.state,
                 observed_at = excluded.observed_at, reason_code = excluded.reason_code""",
            (destination_id, revision, now),
        )

    def revision_is_verified_and_available(
        self,
        conn: sqlite3.Connection,
        destination_id: str,
        revision: str,
    ) -> bool:
        row = conn.execute(
            """SELECT
                 EXISTS(
                   SELECT 1 FROM notification_deliveries latest
                   JOIN notification_destination_operations operation
                     ON operation.operation_id = latest.id AND operation.action = 'test'
                   WHERE latest.destination = ? AND latest.destination_revision = ?
                     AND latest.is_test = 1 AND latest.status = 'sent'
                     AND operation.rowid = (
                       SELECT newest.rowid FROM notification_destination_operations newest
                       JOIN notification_deliveries candidate ON candidate.id = newest.operation_id
                       WHERE newest.action = 'test' AND candidate.destination = ?
                         AND candidate.destination_revision = ? AND candidate.is_test = 1
                       ORDER BY newest.created_at DESC, newest.rowid DESC LIMIT 1
                     )
                 ) AS verified,
                 EXISTS(
                   SELECT 1 FROM notification_destination_availability
                   WHERE destination_id = ? AND revision = ? AND state = 'available'
                 ) AS available""",
            (destination_id, revision, destination_id, revision, destination_id, revision),
        ).fetchone()
        return bool(row["verified"] and row["available"])

    def _verification(self, conn: sqlite3.Connection, destination_id: str, revision: str) -> JSON:
        row = conn.execute(
            """SELECT delivery.id, delivery.destination_revision, delivery.status,
                      delivery.updated_at, delivery.last_reason_code
               FROM notification_deliveries delivery
               JOIN notification_destination_operations operation
                 ON operation.operation_id = delivery.id AND operation.action = 'test'
               WHERE delivery.destination = ? AND delivery.is_test = 1
               ORDER BY (delivery.destination_revision = ?) DESC,
                        operation.created_at DESC, operation.rowid DESC LIMIT 1""",
            (destination_id, revision),
        ).fetchone()
        if row is None:
            return _unverified(revision)
        row_revision = str(row["destination_revision"])
        if row_revision != revision:
            return {
                "operation_id": str(row["id"]),
                "state": "stale",
                "revision": row_revision,
                "checked_at": float(row["updated_at"]),
                "reason_code": "configuration_changed",
            }
        status = str(row["status"])
        state = "verified" if status == "sent" else "failed" if status == "dead_letter" else "verifying"
        return {
            "operation_id": str(row["id"]),
            "state": state,
            "revision": revision,
            "checked_at": float(row["updated_at"]) if state in {"verified", "failed"} else None,
            "reason_code": row["last_reason_code"] if state == "failed" else None,
        }

    def _availability(
        self,
        conn: sqlite3.Connection,
        destination_id: str,
        revision: str,
        verification: JSON,
    ) -> JSON:
        row = conn.execute(
            "SELECT revision, state, observed_at, reason_code FROM notification_destination_availability WHERE destination_id = ?",
            (destination_id,),
        ).fetchone()
        if row is None:
            reason = "configuration_changed" if verification["state"] == "stale" else "test_required"
            return {"state": "unavailable", "observed_at": None, "reason_code": reason}
        if str(row["revision"]) != revision:
            return {
                "state": "unavailable",
                "observed_at": float(row["observed_at"]),
                "reason_code": "configuration_changed",
            }
        return {
            "state": str(row["state"]),
            "observed_at": float(row["observed_at"]),
            "reason_code": row["reason_code"],
        }

    def _pilot_route_selected(
        self,
        conn: sqlite3.Connection,
        destination_id: str,
    ) -> bool:
        row = conn.execute(
            """SELECT enabled, destination_ids_json, selected_destination_revision
               FROM notification_routes WHERE id = ?""",
            (PILOT_ROUTE_ID,),
        ).fetchone()
        return bool(
            row is not None
            and row["enabled"]
            and row["destination_ids_json"] == f'["{destination_id}"]'
        )


def _unverified(revision: str) -> JSON:
    return {
        "operation_id": None,
        "state": "unverified",
        "revision": revision,
        "checked_at": None,
        "reason_code": "test_required",
    }
