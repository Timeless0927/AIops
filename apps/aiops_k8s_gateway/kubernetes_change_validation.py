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

from .connector_enrollments import ConnectorEnrollments
from .connector_validation_commands import ConnectorValidationCommands
from .gateway_db import register_migrations

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


class KubernetesChangeValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class KubernetesChangeValidation:
    """Owns pre-Approval validation state; Kubernetes I/O remains Connector-owned."""

    def __init__(
        self,
        *,
        commands: ConnectorValidationCommands,
        enrollments: ConnectorEnrollments,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        self._commands = commands
        self._enrollments = enrollments
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
            status = "failed" if policy_error else "pending"
            if policy_error is None:
                command_id = self._commands.queue_in(
                    conn,
                    connector_id=connector_id,
                    cluster_id=cluster_id,
                    change=change,
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
            target = change["target"]
            assert isinstance(target, dict)
            api_surface_changed = api_surface_changed or target.get("kind") in {
                "CustomResourceDefinition", "APIService",
            }
        pending = conn.execute(
            "SELECT 1 FROM kubernetes_change_validations WHERE revision_id = ? AND status = 'pending'",
            (revision_id,),
        ).fetchone()
        return "pending" if pending else "failed"

    def supersede_revision_in(self, conn: sqlite3.Connection, revision_id: str, now: float) -> None:
        conn.execute(
            "UPDATE kubernetes_change_validations SET status = 'superseded', updated_at = ? WHERE revision_id = ? AND status = 'pending'",
            (now, revision_id),
        )

    @staticmethod
    def approval_results_in(
        conn: sqlite3.Connection, revision_id: str,
    ) -> dict[str, object] | None:
        rows = conn.execute(
            """
            SELECT ordinal, cluster_id, status, result_json, updated_at
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
        remaining = conn.execute(
            "SELECT 1 FROM kubernetes_change_validations WHERE revision_id = ? AND status = 'pending'",
            (row["revision_id"],),
        ).fetchone()
        if remaining is None:
            failed = conn.execute(
                "SELECT 1 FROM kubernetes_change_validations WHERE revision_id = ? AND status = 'failed'",
                (row["revision_id"],),
            ).fetchone()
            return (
                str(row["change_request_id"]),
                "change_request.validation_failed" if failed else "change_request.validation_succeeded",
                str(row["revision_id"]),
            )
        return None


def validation_projection_in(conn: sqlite3.Connection, revision_id: str) -> dict[str, object] | None:
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
            }
            for row in rows
        ],
    }


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
