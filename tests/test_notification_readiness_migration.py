from __future__ import annotations

import sqlite3
from pathlib import Path

from notification_service import database


def test_v7_migration_preserves_existing_delivery_and_adds_readiness_state(tmp_path: Path) -> None:
    db_path = tmp_path / "notification.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
        )
        for version, schema in database.MIGRATIONS:
            if version >= 7:
                break
            conn.executescript(schema)
            conn.execute("INSERT INTO schema_migrations VALUES (?, 1700000000)", (version,))
        conn.execute(
            """INSERT INTO notification_destinations
               (id, name, provider, config_ciphertext, enabled, tested_at, created_at, updated_at)
               VALUES ('destination:existing', 'Existing', 'feishu', 'ciphertext', 1, 1699999999, 1699999990, 1699999999)"""
        )
        conn.execute(
            """INSERT INTO notification_requests
               (event_id, content_hash, request_json, accepted_at, request_id)
               VALUES ('event:existing', ?, '{}', 1699999999, 'request:existing')""",
            ("a" * 64,),
        )
        conn.execute(
            """INSERT INTO notification_deliveries
               (id, event_id, destination, status, attempt_count, next_attempt_at, updated_at,
                noise_result, redelivery_count)
               VALUES ('delivery:existing', 'event:existing', 'destination:existing', 'pending',
                       0, 1699999999, 1699999999, 'immediate', 0)"""
        )

    database.migrate_notification_database(db_path)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        destination = conn.execute(
            "SELECT revision, tested_at FROM notification_destinations WHERE id = 'destination:existing'"
        ).fetchone()
        delivery = conn.execute(
            """SELECT destination_revision, is_test, paused_reason, last_reason_code
               FROM notification_deliveries WHERE id = 'delivery:existing'"""
        ).fetchone()
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }

    assert destination is not None
    assert str(destination["revision"]).startswith("notification-destination-revision:")
    assert destination["tested_at"] == 1699999999
    assert delivery is not None
    assert delivery["destination_revision"] == destination["revision"]
    assert dict(delivery)["is_test"] == 0
    assert delivery["paused_reason"] is None
    assert delivery["last_reason_code"] is None
    assert {
        "notification_destination_availability",
        "notification_destination_operations",
    } <= tables
