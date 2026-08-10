"""Gateway-owned Kubernetes Change validation state and Connector command queueing."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from collections.abc import Callable

from aiops.contracts import (
    KubernetesChangeContractError,
    validate_draft_kubernetes_change,
    validate_kubernetes_validation_result,
)
from toolsets.query_guard import validate_loki_query, validate_prometheus_query

from aiops.domain.identity import IdentityError

from .connector_commands import ConnectorCommands
from . import connector_command_schema as _command_schema  # noqa: F401
from .connector_enrollments import ConnectorEnrollments
from .gateway_db import register_migrations
from .secure_inputs import SecureInputError, SecureInputs

_SCHEMA_VERSION = 21
_SCHEMA = """
CREATE TABLE kubernetes_change_validations (
    id TEXT PRIMARY KEY,
    change_request_id TEXT NOT NULL REFERENCES change_requests(id) ON DELETE CASCADE,
    phase_id TEXT NOT NULL REFERENCES change_plan_phases(id) ON DELETE CASCADE,
    revision_id TEXT NOT NULL REFERENCES change_plan_revisions(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
    cluster_id TEXT NOT NULL REFERENCES clusters(cluster_id),
    command_id TEXT UNIQUE REFERENCES connector_commands(id),
    draft_json TEXT NOT NULL CHECK (json_valid(draft_json)),
    status TEXT NOT NULL CHECK (status IN ('pending', 'succeeded', 'failed', 'superseded')),
    policy_error_json TEXT CHECK (policy_error_json IS NULL OR json_valid(policy_error_json)),
    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(revision_id, ordinal)
);
CREATE INDEX kubernetes_change_validations_by_command
    ON kubernetes_change_validations(command_id) WHERE command_id IS NOT NULL;
"""
register_migrations(((_SCHEMA_VERSION, _SCHEMA),))

_SECURE_INPUT_BINDING_SCHEMA_VERSION = 31
_SECURE_INPUT_BINDING_SCHEMA = """
CREATE TABLE kubernetes_change_validation_secure_inputs (
    validation_id TEXT NOT NULL REFERENCES kubernetes_change_validations(id) ON DELETE CASCADE,
    secure_input_id TEXT NOT NULL REFERENCES secure_inputs(id),
    PRIMARY KEY (validation_id, secure_input_id)
);
"""
register_migrations(((_SECURE_INPUT_BINDING_SCHEMA_VERSION, _SECURE_INPUT_BINDING_SCHEMA),))


class KubernetesChangeValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class KubernetesChangeValidation:
    """Owns pre-Approval validation state; Kubernetes I/O remains Connector-owned."""

    def __init__(
        self,
        *,
        commands: ConnectorCommands,
        enrollments: ConnectorEnrollments,
        secure_inputs: SecureInputs | None = None,
        availability_recorder: Callable[..., None] | None = None,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        self._commands = commands
        self._enrollments = enrollments
        self._secure_inputs = secure_inputs
        self._availability_recorder = availability_recorder
        self._id_factory = id_factory or (lambda prefix: f"{prefix}-{uuid.uuid4().hex}")

    def begin_in(
        self,
        conn: sqlite3.Connection,
        *,
        change_request_id: str,
        phase_id: str,
        revision_id: str,
        cluster_id: str,
        plan: dict[str, object],
        now: float,
    ) -> str:
        try:
            connector_id = self._enrollments.validation_connector_in(conn, cluster_id)
        except IdentityError as exc:
            raise KubernetesChangeValidationError(exc.code, exc.message) from exc
        changes = plan.get("changes")
        if not isinstance(changes, list):
            raise KubernetesChangeValidationError("invalid_plan", "Change Plan changes are invalid")
        api_surface_changed = False
        actor_row = conn.execute(
            "SELECT actor_id FROM change_requests WHERE id = ?", (change_request_id,),
        ).fetchone()
        if actor_row is None:
            raise KubernetesChangeValidationError("not_found", "Change Request not found")
        secure_input_unavailable = False
        for ordinal, raw_change in enumerate(changes, 1):
            change = validate_draft_kubernetes_change(raw_change)
            policy_error = (
                {
                    "code": "api_surface_change_requires_new_phase",
                    "message": (
                        "Changes after a CustomResourceDefinition or APIService require "
                        "a new Phase with fresh discovery and dry-run"
                    ),
                }
                if api_surface_changed else _query_policy_error(change)
            )
            validation_id = self._id_factory("change-validation")
            command_id = None
            secure_refs: list[dict[str, object]] = []
            if policy_error is None:
                try:
                    if self._secure_inputs is None:
                        from aiops.security import contains_secure_input_placeholder
                        if contains_secure_input_placeholder(change):
                            raise SecureInputError(
                                "secure_input_unavailable", "Secure Input owner is unavailable",
                            )
                    else:
                        secure_refs = self._secure_inputs.encrypted_refs_for_value_in(
                            conn, actor_id=str(actor_row["actor_id"]), value=change,
                        )
                except SecureInputError as exc:
                    policy_error = {"code": exc.code, "message": exc.message}
                    secure_input_unavailable = (
                        secure_input_unavailable or exc.code == "secure_input_unavailable"
                    )
            status = "failed" if policy_error else "pending"
            if policy_error is None:
                command_id = self._commands.queue_validation_in(
                    conn,
                    connector_id=connector_id,
                    cluster_id=cluster_id,
                    change=change,
                    secure_inputs=secure_refs,
                    now=now,
                )
            conn.execute(
                """
                INSERT INTO kubernetes_change_validations (
                    id, change_request_id, phase_id, revision_id, ordinal, cluster_id,
                    command_id, draft_json, status, policy_error_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    validation_id, change_request_id, phase_id, revision_id, ordinal, cluster_id,
                    command_id, _json(change), status, _json(policy_error) if policy_error else None,
                    now, now,
                ),
            )
            if secure_refs:
                conn.executemany(
                    "INSERT INTO kubernetes_change_validation_secure_inputs "
                    "(validation_id, secure_input_id) VALUES (?, ?)",
                    [(validation_id, str(ref["id"])) for ref in secure_refs],
                )
                assert self._secure_inputs is not None
                self._secure_inputs.hold_revision_in(conn, revision_id, secure_refs)
            target = change["target"]
            assert isinstance(target, dict)
            api_surface_changed = api_surface_changed or target.get("kind") in {
                "CustomResourceDefinition", "APIService",
            }
        if secure_input_unavailable and self._availability_recorder is not None:
            self._availability_recorder(
                conn,
                change_request_id=change_request_id,
                phase_id=phase_id,
                execution_id=None,
                command_id=None,
                now=now,
            )
        pending = conn.execute(
            "SELECT 1 FROM kubernetes_change_validations WHERE revision_id = ? AND status = 'pending'",
            (revision_id,),
        ).fetchone()
        return "pending" if pending else "failed"

    def supersede_revision_in(self, conn: sqlite3.Connection, revision_id: str, now: float) -> None:
        commands = conn.execute(
            "SELECT command_id FROM kubernetes_change_validations "
            "WHERE revision_id = ? AND command_id IS NOT NULL",
            (revision_id,),
        ).fetchall()
        conn.execute(
            "UPDATE kubernetes_change_validations SET status = 'superseded', updated_at = ? WHERE revision_id = ? AND status = 'pending'",
            (now, revision_id),
        )
        command_ids = [str(row["command_id"]) for row in commands]
        if command_ids:
            self._commands.reject_in(conn, command_ids, now=now)
            for command_id in command_ids:
                self._commands.redact_secure_inputs_in(conn, command_id)
        self.release_revision_in(conn, revision_id, now=now, delete_after=now)

    def release_revision_in(
        self,
        conn: sqlite3.Connection,
        revision_id: str,
        *,
        now: float,
        delete_after: float,
    ) -> None:
        if self._secure_inputs is not None:
            self._secure_inputs.release_revision_in(
                conn, revision_id, released_at=now, delete_after=delete_after,
            )

    def approval_results_in(
        self, conn: sqlite3.Connection, revision_id: str,
    ) -> dict[str, object] | None:
        rows = conn.execute(
            """
            SELECT id, ordinal, cluster_id, status, draft_json, result_json, updated_at
            FROM kubernetes_change_validations WHERE revision_id = ? ORDER BY ordinal
            """,
            (revision_id,),
        ).fetchall()
        if not rows or any(row["status"] != "succeeded" or row["result_json"] is None for row in rows):
            return None
        cluster_ids = {str(row["cluster_id"]) for row in rows}
        if len(cluster_ids) != 1:
            return None
        return {
            "cluster_id": cluster_ids.pop(),
            "changes": [
                {
                    "ordinal": int(row["ordinal"]), "validated_at": float(row["updated_at"]),
                    "result": json.loads(str(row["result_json"])),
                    "rollback": json.loads(str(row["draft_json"])).get("rollback"),
                    "secure_inputs": self._public_refs_for_validation_in(
                        conn, str(row["id"]),
                    ),
                }
                for row in rows
            ],
        }

    @staticmethod
    def revision_cluster_in(conn: sqlite3.Connection, revision_id: str) -> str | None:
        rows = conn.execute(
            "SELECT DISTINCT cluster_id FROM kubernetes_change_validations WHERE revision_id = ?",
            (revision_id,),
        ).fetchall()
        return str(rows[0]["cluster_id"]) if len(rows) == 1 else None

    def execution_refs_for_revision_in(
        self, conn: sqlite3.Connection, revision_id: str,
    ) -> dict[int, list[dict[str, str]]]:
        if self._secure_inputs is None:
            return {}
        rows = conn.execute(
            """
            SELECT validation.ordinal, binding.secure_input_id
            FROM kubernetes_change_validations validation
            JOIN kubernetes_change_validation_secure_inputs binding
              ON binding.validation_id = validation.id
            WHERE validation.revision_id = ?
            ORDER BY validation.ordinal, binding.secure_input_id
            """,
            (revision_id,),
        ).fetchall()
        ids_by_ordinal: dict[int, list[str]] = {}
        for row in rows:
            ids_by_ordinal.setdefault(int(row["ordinal"]), []).append(
                str(row["secure_input_id"]),
            )
        return {
            ordinal: self._secure_inputs.execution_refs_for_ids_in(conn, input_ids)
            for ordinal, input_ids in ids_by_ordinal.items()
        }

    def projection_in(
        self, conn: sqlite3.Connection, revision_id: str,
    ) -> dict[str, object] | None:
        rows = conn.execute(
            "SELECT * FROM kubernetes_change_validations WHERE revision_id = ? ORDER BY ordinal",
            (revision_id,),
        ).fetchall()
        if not rows:
            return None
        statuses = {str(row["status"]) for row in rows}
        status = "failed" if "failed" in statuses else "succeeded" if statuses == {"succeeded"} else "pending"
        return {
            "status": status,
            "changes": [
                {
                    "ordinal": int(row["ordinal"]),
                    "status": str(row["status"]),
                    "command_id": str(row["command_id"]) if row["command_id"] else None,
                    "policy_error": json.loads(str(row["policy_error_json"])) if row["policy_error_json"] else None,
                    "result": json.loads(str(row["result_json"])) if row["result_json"] else None,
                    "secure_inputs": self._public_refs_for_validation_in(conn, str(row["id"])),
                }
                for row in rows
            ],
        }

    def _public_refs_for_validation_in(
        self, conn: sqlite3.Connection, validation_id: str,
    ) -> list[dict[str, str]]:
        if self._secure_inputs is None:
            return []
        rows = conn.execute(
            "SELECT secure_input_id FROM kubernetes_change_validation_secure_inputs "
            "WHERE validation_id = ? ORDER BY secure_input_id",
            (validation_id,),
        ).fetchall()
        return self._secure_inputs.public_refs_for_ids_in(
            conn, [str(row["secure_input_id"]) for row in rows],
        )

    def record_result_in(
        self,
        conn: sqlite3.Connection,
        command_id: str,
        result: dict[str, object],
        now: float,
    ) -> tuple[str, str, str] | None:
        row = conn.execute(
            "SELECT * FROM kubernetes_change_validations WHERE command_id = ?",
            (command_id,),
        ).fetchone()
        if row is None or row["status"] != "pending":
            return None
        self._commands.redact_secure_inputs_in(conn, command_id)
        policy_error: dict[str, object] | None = None
        validated: dict[str, object] | None = None
        if result.get("status") == "succeeded":
            try:
                raw = json.loads(str(result.get("stdout") or ""))
                validated = validate_kubernetes_validation_result(raw, json.loads(str(row["draft_json"])))
            except (json.JSONDecodeError, KubernetesChangeContractError, TypeError, ValueError) as exc:
                policy_error = {"code": "invalid_validation_result", "message": str(exc)[:500]}
        else:
            policy_error = {
                "code": str(result.get("error_code") or "kubernetes_validation_failed"),
                "message": str(result.get("error_message") or "Kubernetes validation failed")[:500],
            }
        status = "succeeded" if validated is not None else "failed"
        conn.execute(
            """
            UPDATE kubernetes_change_validations
            SET status = ?, policy_error_json = ?, result_json = ?, updated_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (
                status,
                _json(policy_error) if policy_error else None,
                _json(validated) if validated else None,
                now,
                row["id"],
            ),
        )
        if (
            policy_error
            and policy_error["code"] == "secure_input_unavailable"
            and self._availability_recorder is not None
        ):
            self._availability_recorder(
                conn,
                change_request_id=str(row["change_request_id"]),
                phase_id=str(row["phase_id"]),
                execution_id=None,
                command_id=command_id,
                now=now,
            )
            self.release_revision_in(conn, str(row["revision_id"]), now=now, delete_after=now)
        remaining = conn.execute(
            "SELECT 1 FROM kubernetes_change_validations WHERE revision_id = ? AND status = 'pending'",
            (row["revision_id"],),
        ).fetchone()
        if remaining is None:
            failed = conn.execute(
                "SELECT 1 FROM kubernetes_change_validations WHERE revision_id = ? AND status = 'failed'",
                (row["revision_id"],),
            ).fetchone()
            if failed is not None:
                self.release_revision_in(
                    conn, str(row["revision_id"]), now=now, delete_after=now,
                )
            return (
                str(row["change_request_id"]),
                "change_request.validation_failed" if failed else "change_request.validation_succeeded",
                str(row["revision_id"]),
            )
        return None


def _query_policy_error(change: dict[str, object]) -> dict[str, object] | None:
    checks = change.get("post_checks")
    assert isinstance(checks, list)
    for check in checks:
        assert isinstance(check, dict)
        if check.get("type") == "prometheus":
            decision = asyncio.run(validate_prometheus_query(
                str(check["query"]), str(check["start"]), str(check["end"]),
            ))
        elif check.get("type") == "loki":
            decision = asyncio.run(validate_loki_query(
                str(check["query"]), str(check["start"]), str(check["end"]),
                int(check["limit"]) if "limit" in check else None,
            ))
        else:
            continue
        if not decision.get("allowed"):
            return {"code": "post_check_query_rejected", "message": str(decision.get("message") or "query rejected")}
    return None


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
