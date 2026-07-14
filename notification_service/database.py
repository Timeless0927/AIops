"""Notification Engine SQLite schema and migration infrastructure."""

from __future__ import annotations

import sqlite3
from pathlib import Path


_SCHEMA_V1 = """
CREATE TABLE notification_requests (
    event_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
    request_json TEXT NOT NULL CHECK (json_valid(request_json)),
    accepted_at REAL NOT NULL
);
CREATE TABLE notification_deliveries (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    destination TEXT NOT NULL CHECK (destination = 'builtin-fake'),
    status TEXT NOT NULL CHECK (status IN ('pending', 'failed', 'sent', 'dead_letter')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at REAL NOT NULL,
    last_error TEXT,
    message_id TEXT,
    updated_at REAL NOT NULL,
    FOREIGN KEY (event_id) REFERENCES notification_requests(event_id)
);
CREATE INDEX notification_deliveries_due ON notification_deliveries(status, next_attempt_at);
"""
_SCHEMA_V2 = """
ALTER TABLE notification_deliveries RENAME TO notification_deliveries_v1;
DROP INDEX notification_deliveries_due;
CREATE TABLE notification_deliveries (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    destination TEXT NOT NULL CHECK (destination = 'builtin-fake'),
    status TEXT NOT NULL CHECK (status IN ('pending', 'delivering', 'failed', 'sent', 'dead_letter')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at REAL NOT NULL,
    lease_id TEXT,
    lease_until REAL,
    last_error TEXT,
    message_id TEXT,
    updated_at REAL NOT NULL,
    FOREIGN KEY (event_id) REFERENCES notification_requests(event_id)
);
INSERT INTO notification_deliveries (
    id, event_id, destination, status, attempt_count, next_attempt_at,
    last_error, message_id, updated_at
)
SELECT id, event_id, destination, status, attempt_count, next_attempt_at,
       last_error, message_id, updated_at
FROM notification_deliveries_v1;
DROP TABLE notification_deliveries_v1;
CREATE INDEX notification_deliveries_due ON notification_deliveries(status, next_attempt_at, lease_until);
"""
_SCHEMA_V3 = """
ALTER TABLE notification_deliveries RENAME TO notification_deliveries_v2;
DROP INDEX notification_deliveries_due;
CREATE TABLE notification_deliveries (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    destination TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'delivering', 'failed', 'sent', 'dead_letter')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at REAL NOT NULL,
    lease_id TEXT,
    lease_until REAL,
    last_error TEXT,
    message_id TEXT,
    updated_at REAL NOT NULL,
    UNIQUE (event_id, destination),
    FOREIGN KEY (event_id) REFERENCES notification_requests(event_id)
);
INSERT INTO notification_deliveries SELECT * FROM notification_deliveries_v2;
DROP TABLE notification_deliveries_v2;
CREATE INDEX notification_deliveries_due ON notification_deliveries(status, next_attempt_at, lease_until);
CREATE TABLE notification_destinations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    provider TEXT NOT NULL CHECK (provider IN ('feishu', 'dingtalk', 'smtp')),
    config_ciphertext TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    tested_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE notification_routes (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    priority INTEGER NOT NULL UNIQUE,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    match_json TEXT NOT NULL CHECK (json_valid(match_json)),
    destination_ids_json TEXT NOT NULL CHECK (json_valid(destination_ids_json)),
    suppress_reason TEXT,
    is_default INTEGER NOT NULL CHECK (is_default IN (0, 1)),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
INSERT INTO notification_routes VALUES (
    'route:default-suppress', 'Default suppress', 2147483647, 1, '{}', '[]',
    'No notification destination configured', 1, strftime('%s', 'now'), strftime('%s', 'now')
);
CREATE TABLE notification_route_results (
    event_id TEXT PRIMARY KEY,
    route_id TEXT NOT NULL,
    suppressed_reason TEXT,
    routed_at REAL NOT NULL,
    FOREIGN KEY (event_id) REFERENCES notification_requests(event_id),
    FOREIGN KEY (route_id) REFERENCES notification_routes(id)
);
"""
_SCHEMA_V4 = """
CREATE TABLE notification_templates (
    id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    name TEXT NOT NULL,
    provider TEXT NOT NULL CHECK (provider IN ('feishu', 'dingtalk', 'smtp')),
    event_type TEXT NOT NULL,
    is_builtin INTEGER NOT NULL CHECK (is_builtin IN (0, 1)),
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    validated_at REAL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    color TEXT NOT NULL,
    button_label TEXT NOT NULL,
    subject TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (id, version)
);
ALTER TABLE notification_routes ADD COLUMN template_id TEXT;
ALTER TABLE notification_deliveries ADD COLUMN template_id TEXT;
ALTER TABLE notification_deliveries ADD COLUMN template_version INTEGER;
ALTER TABLE notification_deliveries ADD COLUMN presentation_json TEXT CHECK (presentation_json IS NULL OR json_valid(presentation_json));
"""
_SCHEMA_V5 = """
ALTER TABLE notification_deliveries RENAME TO notification_deliveries_v4;
DROP INDEX notification_deliveries_due;
CREATE TABLE notification_deliveries (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    destination TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'delivering', 'failed', 'sent', 'dead_letter', 'suppressed')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at REAL,
    lease_id TEXT,
    lease_until REAL,
    last_error TEXT,
    message_id TEXT,
    updated_at REAL NOT NULL,
    template_id TEXT,
    template_version INTEGER,
    presentation_json TEXT CHECK (presentation_json IS NULL OR json_valid(presentation_json)),
    noise_result TEXT NOT NULL CHECK (noise_result IN ('immediate', 'quiet_hours', 'hourly_limit', 'digest', 'silence')),
    noise_reason TEXT,
    UNIQUE (event_id, destination),
    FOREIGN KEY (event_id) REFERENCES notification_requests(event_id)
);
INSERT INTO notification_deliveries
SELECT id, event_id, destination, status, attempt_count, next_attempt_at, lease_id, lease_until,
       last_error, message_id, updated_at, template_id, template_version, presentation_json, 'immediate', NULL
FROM notification_deliveries_v4;
DROP TABLE notification_deliveries_v4;
CREATE INDEX notification_deliveries_due ON notification_deliveries(status, next_attempt_at, lease_until);
CREATE TABLE notification_destination_noise_controls (
    destination_id TEXT PRIMARY KEY,
    timezone TEXT NOT NULL,
    quiet_start TEXT,
    quiet_end TEXT,
    hourly_limit INTEGER CHECK (hourly_limit IS NULL OR hourly_limit > 0),
    digest_interval_seconds INTEGER CHECK (digest_interval_seconds IS NULL OR digest_interval_seconds >= 60),
    updated_at REAL NOT NULL,
    FOREIGN KEY (destination_id) REFERENCES notification_destinations(id) ON DELETE CASCADE
);
CREATE TABLE notification_silences (
    id TEXT PRIMARY KEY,
    match_json TEXT NOT NULL CHECK (json_valid(match_json)),
    reason TEXT NOT NULL,
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX notification_silences_active ON notification_silences(expires_at);
"""
_SCHEMA_V6 = """
ALTER TABLE notification_requests ADD COLUMN request_id TEXT;
ALTER TABLE notification_deliveries ADD COLUMN redelivery_count INTEGER NOT NULL DEFAULT 0 CHECK (redelivery_count >= 0);
CREATE TABLE notification_delivery_attempts (
    id TEXT PRIMARY KEY,
    delivery_id TEXT NOT NULL,
    redelivery_count INTEGER NOT NULL CHECK (redelivery_count >= 0),
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    outcome TEXT NOT NULL CHECK (outcome IN ('delivering', 'failed', 'sent', 'dead_letter')),
    retryable INTEGER CHECK (retryable IS NULL OR retryable IN (0, 1)),
    error TEXT,
    started_at REAL NOT NULL,
    completed_at REAL,
    UNIQUE (delivery_id, redelivery_count, attempt_number),
    FOREIGN KEY (delivery_id) REFERENCES notification_deliveries(id)
);
"""
_SCHEMA_V7 = """
ALTER TABLE notification_destinations ADD COLUMN revision TEXT;
UPDATE notification_destinations
SET revision = 'notification-destination-revision:' || lower(hex(randomblob(16)))
WHERE revision IS NULL;
CREATE UNIQUE INDEX notification_destination_revision ON notification_destinations(revision);

ALTER TABLE notification_deliveries ADD COLUMN destination_revision TEXT;
ALTER TABLE notification_deliveries ADD COLUMN is_test INTEGER NOT NULL DEFAULT 0 CHECK (is_test IN (0, 1));
ALTER TABLE notification_deliveries ADD COLUMN paused_reason TEXT;
ALTER TABLE notification_deliveries ADD COLUMN last_reason_code TEXT;
UPDATE notification_deliveries
SET destination_revision = (
    SELECT revision FROM notification_destinations
    WHERE notification_destinations.id = notification_deliveries.destination
)
WHERE destination != 'builtin-fake';

ALTER TABLE notification_routes ADD COLUMN selected_destination_revision TEXT;

CREATE TABLE notification_destination_availability (
    destination_id TEXT PRIMARY KEY,
    revision TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('available', 'degraded', 'unavailable')),
    observed_at REAL NOT NULL,
    reason_code TEXT,
    FOREIGN KEY (destination_id) REFERENCES notification_destinations(id) ON DELETE CASCADE
);

CREATE TABLE notification_destination_operations (
    operation_id TEXT PRIMARY KEY,
    mutation_hash TEXT NOT NULL CHECK (length(mutation_hash) = 64),
    action TEXT NOT NULL,
    destination_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY (destination_id) REFERENCES notification_destinations(id) ON DELETE CASCADE
);
"""
MIGRATIONS = (
    (1, _SCHEMA_V1), (2, _SCHEMA_V2), (3, _SCHEMA_V3), (4, _SCHEMA_V4),
    (5, _SCHEMA_V5), (6, _SCHEMA_V6), (7, _SCHEMA_V7),
)


def migrate_notification_database(db_path: Path | str) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5)
    try:
        with conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
            )
            applied = {int(row[0]) for row in conn.execute("SELECT version FROM schema_migrations")}
            for version, schema in MIGRATIONS:
                if version in applied:
                    continue
                conn.executescript(
                    f"BEGIN IMMEDIATE;\n{schema}\nINSERT INTO schema_migrations VALUES ({version}, strftime('%s', 'now'));\nCOMMIT;"
                )
    finally:
        conn.close()

