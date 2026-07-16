"""Result classification, rollback construction, and projection for Kubernetes Plans."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from typing import Any

from .kubernetes_execution_codec import canonical_digest as _digest_json, canonical_json as _json
from .kubernetes_inverse_changes import KubernetesInverseChangeError, bind_inverse_change


def result_outcome(result: dict[str, object]) -> tuple[str, str | None]:
    error_code = str(result["error_code"]) if result.get("error_code") else None
    outcome = (
        "succeeded" if result.get("status") == "succeeded"
        else "unknown_outcome" if error_code == "execution_outcome_unknown"
        else "stale" if error_code == "stale_change"
        else "post_check_failed" if error_code == "post_check_failed"
        else "failed"
    )
    return outcome, error_code


def validate_declared_execution_result(change_json: str, result: dict[str, object]) -> None:
    execution = result.get("execution")
    if execution is None:
        return
    change = json.loads(change_json)
    if not isinstance(execution, dict) or not isinstance(change, dict):
        raise ValueError("typed execution result is invalid")
    declared = change.get("post_checks")
    actual = execution.get("post_checks")
    if (
        execution.get("operation") != change.get("operation")
        or not isinstance(declared, list)
        or not isinstance(actual, list)
        or [item.get("type") for item in actual if isinstance(item, dict)]
        != [item.get("type") for item in declared if isinstance(item, dict)]
        or len(actual) != len(declared)
    ):
        raise ValueError("typed execution result does not match declared post-checks")


def create_rollback_steps_in(
    conn: sqlite3.Connection,
    *,
    execution_id: str,
    failed_step_id: str,
    failed_outcome: str,
    now: float,
    id_factory: Callable[[str], str],
) -> int:
    candidates = conn.execute(
        """
        SELECT * FROM kubernetes_change_execution_steps
        WHERE execution_id = ? AND direction = 'forward'
          AND (status = 'succeeded' OR (id = ? AND ? = 'post_check_failed'))
        ORDER BY ordinal DESC
        """,
        (execution_id, failed_step_id, failed_outcome),
    ).fetchall()
    for rollback_ordinal, source in enumerate(candidates, 1):
        inverse = json.loads(str(source["inverse_change_json"])) if source["inverse_change_json"] else None
        result = json.loads(str(source["result_json"])) if source["result_json"] else None
        execution = _execution_detail(result)
        try:
            bound = bind_inverse_change(inverse, execution)
        except KubernetesInverseChangeError as exc:
            raise KubernetesInverseChangeError(
                f"forward step {source['ordinal']} cannot bind its frozen inverse: {exc}",
            ) from exc
        command_id = id_factory("command")
        conn.execute(
            """
            INSERT INTO kubernetes_change_execution_steps (
                id, execution_id, ordinal, direction, source_step_id, command_id,
                change_hash, change_json, status, created_at
            ) VALUES (?, ?, ?, 'rollback', ?, ?, ?, ?, 'pending', ?)
            """,
            (
                f"{execution_id}:rollback:{rollback_ordinal}", execution_id, rollback_ordinal,
                source["id"], command_id, _digest_json(bound), _json(bound), now,
            ),
        )
    return len(candidates)


def project_steps_in(conn: sqlite3.Connection, execution_id: str) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT step.*, source.ordinal AS source_ordinal
        FROM kubernetes_change_execution_steps step
        LEFT JOIN kubernetes_change_execution_steps source ON source.id = step.source_step_id
        WHERE step.execution_id = ?
        ORDER BY CASE step.direction WHEN 'forward' THEN 0 ELSE 1 END, step.ordinal
        """,
        (execution_id,),
    ).fetchall()
    grants = {
        str(row["step_id"]): row
        for row in conn.execute(
            "SELECT * FROM kubernetes_execution_grants WHERE execution_id = ?", (execution_id,),
        ).fetchall()
    }
    return [_project_step(row, grants.get(str(row["id"]))) for row in rows]


def active_step(steps: list[dict[str, object]]) -> dict[str, object]:
    active = next(
        (step for step in steps if step["status"] in {"queued", "dispatched", "started"}),
        None,
    )
    if active is not None:
        return active
    pending = next((step for step in steps if step["status"] == "pending"), None)
    return pending or steps[-1]


def _execution_detail(result: object) -> dict[str, object] | None:
    value = result.get("execution") if isinstance(result, dict) else None
    return value if isinstance(value, dict) else None


def _project_step(row: Any, grant: Any) -> dict[str, object]:
    return {
        "id": str(row["id"]), "ordinal": int(row["ordinal"]),
        "direction": str(row["direction"]),
        "source_ordinal": int(row["source_ordinal"]) if row["source_ordinal"] is not None else None,
        "command_id": str(row["command_id"]), "status": str(row["status"]),
        "change": json.loads(str(row["change_json"])),
        "started_at": float(row["started_at"]) if row["started_at"] is not None else None,
        "completed_at": float(row["completed_at"]) if row["completed_at"] is not None else None,
        "result": json.loads(str(row["result_json"])) if row["result_json"] else None,
        "grant": {
            "id": str(grant["id"]), "issued_at": float(grant["issued_at"]),
            "expires_at": float(grant["expires_at"]),
            "consumed_at": float(grant["consumed_at"]) if grant["consumed_at"] is not None else None,
            "revoked_at": float(grant["revoked_at"]) if grant["revoked_at"] is not None else None,
        } if grant is not None else None,
    }
