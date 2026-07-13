"""SQLite persistence adapter for Diagnosis Model Provider state."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from diagnosis_service.database import connect, migrate


JSON = dict[str, Any]

_MIGRATIONS = (
    (
        3,
        """
        CREATE TABLE model_provider_revisions (
            revision TEXT PRIMARY KEY,
            endpoint TEXT NOT NULL,
            endpoint_scope TEXT NOT NULL CHECK (endpoint_scope IN ('external', 'cluster_internal')),
            model TEXT NOT NULL,
            timeout_seconds INTEGER NOT NULL CHECK (timeout_seconds BETWEEN 5 AND 120),
            credential_ciphertext TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE model_provider_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            current_revision TEXT NOT NULL REFERENCES model_provider_revisions(revision)
        );
        """,
    ),
    (
        4,
        """
        CREATE TABLE model_provider_verifications (
            operation_id TEXT PRIMARY KEY,
            revision TEXT NOT NULL REFERENCES model_provider_revisions(revision),
            nonce TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'verified', 'failed')),
            reason_code TEXT,
            actor_id TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            lease_until REAL,
            checked_at REAL,
            latency_ms INTEGER,
            provider_summary TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX model_provider_verification_due
            ON model_provider_verifications(state, created_at, operation_id);
        CREATE TABLE model_provider_availability (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            revision TEXT NOT NULL REFERENCES model_provider_revisions(revision),
            state TEXT NOT NULL CHECK (state IN ('available', 'degraded', 'unavailable')),
            observed_at REAL NOT NULL,
            reason_code TEXT
        );
        """,
    ),
    (
        6,
        """
        CREATE TABLE model_provider_mutations (
            operation_id TEXT PRIMARY KEY,
            action TEXT NOT NULL,
            mutation_hash TEXT NOT NULL,
            result_revision TEXT,
            created_at REAL NOT NULL
        );
        """,
    ),
)


class RepositoryConflict(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ModelProviderRepository:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        migrate(self.db_path, _MIGRATIONS)

    def save_revision(
        self,
        revision: JSON,
        *,
        expected_revision: str | None,
        operation_id: str | None,
        mutation_hash: str,
    ) -> None:
        with connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if operation_id:
                mutation = connection.execute(
                    "SELECT action, mutation_hash FROM model_provider_mutations WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                if mutation is not None:
                    if str(mutation["action"]) != "save" or str(mutation["mutation_hash"]) != mutation_hash:
                        raise RepositoryConflict("operation_conflict")
                    return
            current = connection.execute(
                "SELECT current_revision FROM model_provider_state WHERE singleton = 1"
            ).fetchone()
            current_revision = str(current["current_revision"]) if current is not None else None
            if current_revision != expected_revision:
                raise RepositoryConflict("revision_conflict")
            connection.execute(
                """INSERT INTO model_provider_revisions
                   (revision, endpoint, endpoint_scope, model, timeout_seconds,
                    credential_ciphertext, actor_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    revision["revision"], revision["endpoint"], revision["endpoint_scope"],
                    revision["model"], revision["timeout_seconds"], revision["credential_ciphertext"],
                    revision["actor_id"], revision["created_at"],
                ),
            )
            connection.execute(
                """INSERT INTO model_provider_state (singleton, current_revision)
                   VALUES (1, ?) ON CONFLICT(singleton) DO UPDATE
                   SET current_revision = excluded.current_revision""",
                (revision["revision"],),
            )
            if operation_id:
                connection.execute(
                    "INSERT INTO model_provider_mutations VALUES (?, 'save', ?, ?, ?)",
                    (operation_id, mutation_hash, revision["revision"], revision["created_at"]),
                )

    def snapshot(self) -> tuple[JSON | None, JSON | None, JSON | None, JSON | None]:
        with connect(self.db_path) as connection:
            revision = connection.execute(
                """SELECT revisions.* FROM model_provider_state state
                   JOIN model_provider_revisions revisions ON revisions.revision = state.current_revision
                   WHERE state.singleton = 1"""
            ).fetchone()
            latest = None if revision is None else connection.execute(
                """SELECT * FROM model_provider_verifications
                   ORDER BY created_at DESC, rowid DESC LIMIT 1"""
            ).fetchone()
            availability = None if revision is None else connection.execute(
                "SELECT * FROM model_provider_availability WHERE singleton = 1"
            ).fetchone()
            verified = None if revision is None else connection.execute(
                """SELECT * FROM model_provider_verifications
                   WHERE revision = ? AND state = 'verified'
                   ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (str(revision["revision"]),),
            ).fetchone()
        return tuple(_row(item) for item in (revision, latest, availability, verified))  # type: ignore[return-value]

    def start_verification(self, operation: JSON) -> str | None:
        with connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT current_revision FROM model_provider_state WHERE singleton = 1"
            ).fetchone()
            if current is None:
                raise RepositoryConflict("not_configured")
            if str(current["current_revision"]) != operation["revision"]:
                raise RepositoryConflict("revision_conflict")
            existing = connection.execute(
                "SELECT revision, state FROM model_provider_verifications WHERE operation_id = ?",
                (operation["operation_id"],),
            ).fetchone()
            if existing is not None:
                if str(existing["revision"]) != operation["revision"]:
                    raise RepositoryConflict("operation_conflict")
                return str(existing["state"])
            connection.execute(
                """INSERT INTO model_provider_verifications
                   (operation_id, revision, nonce, state, actor_id, created_at, updated_at)
                   VALUES (?, ?, ?, 'queued', ?, ?, ?)""",
                (
                    operation["operation_id"], operation["revision"], operation["nonce"],
                    operation["actor_id"], operation["created_at"], operation["created_at"],
                ),
            )
        return None

    def delete_current(
        self,
        *,
        expected_revision: str,
        operation_id: str | None,
        mutation_hash: str,
        now: float,
    ) -> None:
        with connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if operation_id:
                mutation = connection.execute(
                    "SELECT action, mutation_hash FROM model_provider_mutations WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                if mutation is not None:
                    if str(mutation["action"]) != "delete" or str(mutation["mutation_hash"]) != mutation_hash:
                        raise RepositoryConflict("operation_conflict")
                    return
            current = connection.execute(
                "SELECT current_revision FROM model_provider_state WHERE singleton = 1"
            ).fetchone()
            if current is None or str(current["current_revision"]) != expected_revision:
                raise RepositoryConflict("revision_conflict")
            connection.execute("DELETE FROM model_provider_state WHERE singleton = 1")
            connection.execute("DELETE FROM model_provider_availability WHERE singleton = 1")
            if operation_id:
                connection.execute(
                    "INSERT INTO model_provider_mutations VALUES (?, 'delete', ?, ?, ?)",
                    (operation_id, mutation_hash, expected_revision, now),
                )

    def claim_verification(self, *, now: float, lease_until: float) -> JSON | None:
        with connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT * FROM model_provider_verifications
                   WHERE state = 'queued' OR (state = 'running' AND lease_until <= ?)
                   ORDER BY created_at, operation_id LIMIT 1""",
                (now,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """UPDATE model_provider_verifications
                   SET state = 'running', attempt_count = attempt_count + 1,
                       lease_until = ?, updated_at = ? WHERE operation_id = ?""",
                (lease_until, now, str(row["operation_id"])),
            )
        return _row(row)

    def complete_verification(self, operation_id: str, outcome: JSON) -> None:
        with connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE model_provider_verifications
                   SET state = ?, reason_code = ?, checked_at = ?, latency_ms = ?,
                       provider_summary = ?, lease_until = NULL, updated_at = ?
                   WHERE operation_id = ? AND state = 'running'""",
                (
                    outcome["state"], outcome["reason_code"], outcome["checked_at"],
                    outcome["latency_ms"], outcome["provider_summary"], outcome["checked_at"],
                    operation_id,
                ),
            )
            _upsert_availability(connection, outcome)

    def revision(self, revision: str) -> JSON | None:
        with connect(self.db_path) as connection:
            return _row(connection.execute(
                "SELECT * FROM model_provider_revisions WHERE revision = ?",
                (revision,),
            ).fetchone())

    def record_availability(self, outcome: JSON, *, invalidate_verification: bool) -> None:
        with connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT current_revision FROM model_provider_state WHERE singleton = 1"
            ).fetchone()
            if current is None or str(current["current_revision"]) != outcome["revision"]:
                return
            if invalidate_verification:
                verification = connection.execute(
                    """SELECT operation_id FROM model_provider_verifications
                       WHERE revision = ? ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                    (outcome["revision"],),
                ).fetchone()
                if verification is not None:
                    connection.execute(
                        """UPDATE model_provider_verifications
                           SET state = 'failed', reason_code = ?, checked_at = ?, updated_at = ?
                           WHERE operation_id = ?""",
                        (
                            outcome["reason_code"], outcome["observed_at"], outcome["observed_at"],
                            str(verification["operation_id"]),
                        ),
                    )
            _upsert_availability(connection, outcome)


def _upsert_availability(connection, outcome: JSON) -> None:
    observed_at = outcome.get("observed_at", outcome.get("checked_at"))
    connection.execute(
        """INSERT INTO model_provider_availability
           (singleton, revision, state, observed_at, reason_code)
           VALUES (1, ?, ?, ?, ?) ON CONFLICT(singleton) DO UPDATE SET
           revision = excluded.revision, state = excluded.state,
           observed_at = excluded.observed_at, reason_code = excluded.reason_code""",
        (outcome["revision"], outcome["availability_state"], observed_at, outcome["reason_code"]),
    )


def _row(row) -> JSON | None:
    return dict(row) if row is not None else None
