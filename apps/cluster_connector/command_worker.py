"""Connector-owned durable read command worker and local journal."""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aiops.k8s import CommandEnvelope
from aiops.contracts.connector_journal import terminal_journal_evidence
from aiops.security import public_secure_input_facts

from .gateway_client import connector_gateway_url_is_secure, request_context_headers
from .deployment_mutations import build_mutation_envelopes, deployment_replicas
from .kubernetes_change_adapter import (
    execute_change_command,
    execute_validation_command,
)
from .kubernetes_reconciliation_adapter import execute_reconciliation_command
from .kubectl_executor import execute_command_envelope


_RESOURCE_KINDS = {"pods", "deployments", "services", "events"}
_OUTPUTS = {"json", "yaml", "wide"}
_READ_ACTIONS = {
    "get_resource", "validate_kubernetes_change", "reconcile_kubernetes_change",
}
_JOURNAL_RETENTION_SECONDS = 30 * 24 * 60 * 60
_EXECUTION_LOCK_SECONDS = 60 * 60
_CLEANUP_BATCH_SIZE = 1000
_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS command_journal (
    command_id TEXT PRIMARY KEY,
    command_json TEXT NOT NULL CHECK (json_valid(command_json)),
    state TEXT NOT NULL CHECK (state IN ('accepted', 'started', 'terminal', 'acknowledged')),
    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS execution_locks (
    scope TEXT PRIMARY KEY,
    command_id TEXT NOT NULL UNIQUE,
    acquired_at REAL NOT NULL
);
"""
_MIGRATIONS = (
    (1, _SCHEMA_V1),
    (2, "ALTER TABLE execution_locks ADD COLUMN expires_at REAL;"),
    (3, """
        ALTER TABLE execution_locks RENAME TO execution_locks_v2;
        CREATE TABLE execution_locks (
            scope TEXT PRIMARY KEY,
            command_id TEXT NOT NULL UNIQUE,
            acquired_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            FOREIGN KEY (command_id) REFERENCES command_journal(command_id) ON DELETE CASCADE
        );
        INSERT INTO execution_locks
        SELECT scope, command_id, acquired_at, COALESCE(expires_at, acquired_at + 3600)
        FROM execution_locks_v2;
        DROP TABLE execution_locks_v2;
    """),
    (4, """
        ALTER TABLE command_journal ADD COLUMN execution_grant_id TEXT;
        UPDATE command_journal
        SET execution_grant_id = json_extract(command_json, '$.execution_grant_id')
        WHERE json_extract(command_json, '$.action') = 'execute_kubernetes_change';
        CREATE UNIQUE INDEX command_journal_execution_grant
            ON command_journal(execution_grant_id) WHERE execution_grant_id IS NOT NULL;
    """),
)


class ConnectorCommandJournal:
    def __init__(self, db_path: Path | str, *, clock: Callable[[], float] = time.time) -> None:
        self.db_path = Path(db_path)
        self._clock = clock
        self._migrate()

    def accept(self, command: dict[str, object]) -> str:
        command_id = str(command.get("id") or "")
        if not command_id:
            raise ValueError("Connector Command id is required")
        grant_id = (
            str(command.get("execution_grant_id") or "")
            if command.get("action") == "execute_kubernetes_change" else ""
        )
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT state, command_json FROM command_journal WHERE command_id = ?", (command_id,),
            ).fetchone()
            if existing is not None and str(existing[0]) != "accepted":
                existing_command = json.loads(str(existing[1]))
                if (
                    str(existing[0]) == "started"
                    and command.get("action") in _READ_ACTIONS
                    and existing_command.get("action") == command.get("action")
                ):
                    conn.execute(
                        """
                        UPDATE command_journal
                        SET command_json = ?, state = 'accepted', result_json = NULL, updated_at = ?
                        WHERE command_id = ? AND state = 'started'
                        """,
                        (_json(command), self._clock(), command_id),
                    )
                else:
                    return "already_started"
            prior = conn.execute(
                "SELECT command_id FROM command_journal WHERE execution_grant_id = ?",
                (grant_id,),
            ).fetchone() if grant_id else None
            duplicate_grant = prior is not None and str(prior[0]) != command_id
            conn.execute(
                """
                INSERT INTO command_journal (
                    command_id, command_json, state, updated_at, execution_grant_id
                ) VALUES (?, ?, 'accepted', ?, ?)
                ON CONFLICT(command_id) DO UPDATE SET command_json = excluded.command_json, updated_at = excluded.updated_at
                WHERE command_journal.state IN ('accepted', 'started')
                """,
                (command_id, _json(command), self._clock(), None if duplicate_grant else grant_id or None),
            )
        return "duplicate_grant" if duplicate_grant else "accepted"

    def started(self, command_id: str) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE command_journal SET state = 'started', updated_at = ? WHERE command_id = ? AND state IN ('accepted', 'started')",
                (self._clock(), command_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Connector Command must be accepted before started")

    def redact_accepted(self, command_id: str) -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT command_json FROM command_journal WHERE command_id = ? AND state = 'accepted'",
                (command_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Connector Command must be accepted before transport redaction")
            command = _redact_secure_input_transport(json.loads(str(row[0])))
            conn.execute(
                "UPDATE command_journal SET command_json = ?, updated_at = ? "
                "WHERE command_id = ? AND state = 'accepted'",
                (_json(command), self._clock(), command_id),
            )

    def terminal(self, command_id: str, result: dict[str, object]) -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT command_json FROM command_journal WHERE command_id = ? AND state = 'started'",
                (command_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Connector Command must be started before terminal result")
            command = _redact_secure_input_transport(json.loads(str(row[0])))
            cursor = conn.execute(
                "UPDATE command_journal SET state = 'terminal', command_json = ?, "
                "result_json = ?, updated_at = ? WHERE command_id = ? AND state = 'started'",
                (_json(command), _json(result), self._clock(), command_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Connector Command must be started before terminal result")

    def acknowledged(self, command_id: str) -> None:
        self._transition(command_id, "acknowledged", expected="terminal")

    def unreported_results(self) -> list[tuple[dict[str, object], dict[str, object]]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT command_json, result_json FROM command_journal WHERE state = 'terminal' ORDER BY updated_at"
            ).fetchall()
        return [(json.loads(row[0]), json.loads(row[1])) for row in rows]

    def unreported_results_with_evidence(
        self,
    ) -> list[tuple[dict[str, object], dict[str, object], dict[str, object]]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT command_id, command_json, result_json, updated_at "
                "FROM command_journal WHERE state = 'terminal' ORDER BY updated_at"
            ).fetchall()
        return [
            (
                json.loads(row[1]),
                json.loads(row[2]),
                terminal_journal_evidence(
                    str(row[0]), json.loads(row[2]), recorded_at=float(row[3]),
                ),
            )
            for row in rows
        ]

    def unreported_result(self, command_id: str) -> dict[str, object] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT result_json FROM command_journal WHERE command_id = ? AND state = 'terminal'", (command_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def metrics(self, *, now: float | None = None) -> str:
        observed_at = self._clock() if now is None else now
        with self._connect() as conn:
            counts = dict(conn.execute("SELECT state, COUNT(*) FROM command_journal GROUP BY state"))
            oldest = conn.execute(
                "SELECT MIN(updated_at) FROM command_journal WHERE state IN ('accepted', 'started', 'terminal')"
            ).fetchone()[0]
            cleanup_journal = conn.execute(
                "SELECT COUNT(*) FROM command_journal WHERE state = 'acknowledged' AND updated_at <= ?",
                (observed_at - _JOURNAL_RETENTION_SECONDS,),
            ).fetchone()[0]
            cleanup_locks = conn.execute(
                "SELECT COUNT(*) FROM execution_locks WHERE expires_at <= ?", (observed_at,)
            ).fetchone()[0]
        lines = [
            "# HELP aiops_connector_command_journal Connector journal records by bounded state",
            "# TYPE aiops_connector_command_journal gauge",
        ]
        for state in ("accepted", "started", "terminal", "acknowledged"):
            lines.append(f'aiops_connector_command_journal{{state="{state}"}} {int(counts.get(state, 0))}')
        lines.extend((
            "# HELP aiops_connector_command_oldest_age_seconds Age since the oldest unfinished Connector journal progress",
            "# TYPE aiops_connector_command_oldest_age_seconds gauge",
            f"aiops_connector_command_oldest_age_seconds {max(0.0, observed_at - float(oldest)) if oldest else 0.0:.1f}",
            "# HELP aiops_connector_cleanup_eligible Connector records currently eligible for cleanup",
            "# TYPE aiops_connector_cleanup_eligible gauge",
            f'aiops_connector_cleanup_eligible{{record="journal"}} {int(cleanup_journal)}',
            f'aiops_connector_cleanup_eligible{{record="lock"}} {int(cleanup_locks)}',
        ))
        return "\n".join(lines) + "\n"

    def acquire_execution_lock(self, scope: str, command_id: str) -> bool:
        now = self._clock()
        with self._connect() as conn:
            conn.execute("DELETE FROM execution_locks WHERE expires_at <= ?", (now,))
            conn.execute(
                "INSERT OR IGNORE INTO execution_locks (scope, command_id, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
                (scope, command_id, now, now + _EXECUTION_LOCK_SECONDS),
            )
            return conn.execute("SELECT command_id FROM execution_locks WHERE scope = ?", (scope,)).fetchone()[0] == command_id

    def release_execution_lock(self, scope: str, command_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM execution_locks WHERE scope = ? AND command_id = ?", (scope, command_id))

    def cleanup_expired(self) -> dict[str, int]:
        now = self._clock()
        with self._connect() as conn:
            locks = conn.execute(
                """DELETE FROM execution_locks WHERE rowid IN (
                       SELECT rowid FROM execution_locks WHERE expires_at <= ? LIMIT ?
                   )""",
                (now, _CLEANUP_BATCH_SIZE),
            ).rowcount
            journal = conn.execute(
                """DELETE FROM command_journal WHERE rowid IN (
                       SELECT rowid FROM command_journal
                       WHERE state = 'acknowledged' AND updated_at <= ? LIMIT ?
                   )""",
                (now - _JOURNAL_RETENTION_SECONDS, _CLEANUP_BATCH_SIZE),
            ).rowcount
        return {"journal": journal, "locks": locks}

    def _transition(self, command_id: str, state: str, *, expected: str = "accepted") -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE command_journal SET state = ?, updated_at = ? WHERE command_id = ? AND state = ?",
                (state, self._clock(), command_id, expected),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Connector Command must be {expected} before {state}")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA secure_delete=ON")
        return conn

    def _migrate(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
            )
            applied = {int(row[0]) for row in conn.execute("SELECT version FROM schema_migrations")}
            for version, schema in _MIGRATIONS:
                if version not in applied:
                    conn.executescript(
                        f"BEGIN IMMEDIATE;\n{schema}\n"
                        f"INSERT INTO schema_migrations VALUES ({version}, strftime('%s', 'now'));\nCOMMIT;"
                    )
            conn.execute(
                "UPDATE execution_locks SET expires_at = acquired_at + ? WHERE expires_at IS NULL",
                (_EXECUTION_LOCK_SECONDS,),
            )


def build_read_envelope(command: dict[str, object]) -> CommandEnvelope:
    if set(command) - {
        "id", "cluster_id", "namespace", "action", "parameters", "status", "attempt_count",
        "lease_id", "lease_expires_at", "created_at", "result", "execution_grant_id", "execution_grant_expires_at", "action_hash",
        "rollback_plan",
        "frozen_action",
        "scale_replica_bounds",
    }:
        raise ValueError("unsupported Connector Command fields")
    if command.get("action") != "get_resource" or not isinstance(command.get("parameters"), dict):
        raise ValueError("unsupported read action")
    parameters = command["parameters"]
    if set(parameters) - {"resource_kind", "name", "selector", "output"}:
        raise ValueError("unsupported read parameters")
    resource = parameters.get("resource_kind")
    output = parameters.get("output", "json")
    if not isinstance(resource, str) or not isinstance(output, str) or resource not in _RESOURCE_KINDS or output not in _OUTPUTS:
        raise ValueError("unsupported read parameters")
    argv = ["kubectl", "get", str(resource)]
    name = parameters.get("name")
    selector = parameters.get("selector")
    if name is not None:
        if not isinstance(name, str) or not name.strip() or len(name) > 253 or any(c in name for c in ";|`\n\r"):
            raise ValueError("invalid resource name")
        if resource == "events":
            raise ValueError("events do not accept a resource name")
        argv.append(name.strip())
    if selector is not None:
        if not isinstance(selector, str) or not selector.strip() or len(selector) > 253 or any(c in selector for c in ";|`\n\r"):
            raise ValueError("invalid selector")
        argv.extend(("--selector", selector.strip()))
    namespace = str(command.get("namespace") or "")
    argv.extend(("--namespace", namespace, "--output", str(output)))
    command_id = str(command.get("id") or "")
    return CommandEnvelope(
        envelope_version="v1",
        task_id=command_id,
        command_id=command_id,
        cluster_id=str(command.get("cluster_id") or ""),
        namespace=namespace,
        action_type="read",
        argv=tuple(argv),
        timeout_seconds=30,
        output_limit_bytes=1024 * 1024,
        risk_level="low",
        grant_id=f"read:{command_id}",
    )


def execute_read_command(
    command: dict[str, object], *, connector_id: str, cluster_id: str, allowed_namespaces: set[str]
) -> dict[str, object]:
    result = execute_command_envelope(
        build_read_envelope(command),
        connector_id=connector_id,
        connector_cluster_id=cluster_id,
        allowed_namespaces=allowed_namespaces,
    )
    response = {
        key: result.to_dict().get(key)
        for key in ("status", "stdout", "stderr", "exit_code", "truncated", "error_code", "error_message")
    }
    if response["status"] == "command_rejected":
        response["status"] = "rejected"
    return response


def execute_kubernetes_validation(
    command: dict[str, object], *, cluster_id: str,
    allowed_namespaces: set[str], executor: Callable[..., dict[str, object]] = execute_validation_command,
) -> dict[str, object]:
    result = executor(
        command,
        connector_cluster_id=cluster_id,
        allowed_namespaces=allowed_namespaces,
    )
    if result.get("status") == "succeeded" and isinstance(result.get("validation"), dict):
        stdout = _json(result["validation"])
        if len(stdout.encode()) > 1024 * 1024:
            raise ValueError("Kubernetes validation result exceeds output limit")
        return {
            "status": "succeeded", "stdout": stdout, "stderr": "", "exit_code": 0,
            "truncated": False, "error_code": None, "error_message": None,
        }
    return {
        "status": "rejected", "stdout": "", "stderr": "", "exit_code": None,
        "truncated": False,
        "error_code": str(result.get("error_code") or "kubernetes_validation_rejected"),
        "error_message": str(result.get("error_message") or "Kubernetes validation was rejected")[:500],
    }


def execute_kubernetes_change(
    command: dict[str, object], *, cluster_id: str,
    allowed_namespaces: set[str], now: float,
    clock: Callable[[], float] | None = None,
    executor: Callable[..., dict[str, object]] = execute_change_command,
) -> dict[str, object]:
    result = executor(
        command, connector_cluster_id=cluster_id,
        allowed_namespaces=allowed_namespaces, now=now, clock=clock,
    )
    execution = result.get("execution")
    stdout = _json(execution) if isinstance(execution, dict) else ""
    if len(stdout.encode()) > 1024 * 1024:
        raise ValueError("Kubernetes execution result exceeds output limit")
    status = str(result.get("status") or "failed")
    return {
        "status": status if status in {"succeeded", "failed", "rejected"} else "failed",
        "stdout": stdout, "stderr": "", "exit_code": 0 if status == "succeeded" else None,
        "truncated": False, "error_code": result.get("error_code"),
        "error_message": str(result.get("error_message"))[:500] if result.get("error_message") else None,
    }


def execute_kubernetes_reconciliation(
    command: dict[str, object], *, cluster_id: str,
    allowed_namespaces: set[str], observed_at: float,
    executor: Callable[..., dict[str, object]] = execute_reconciliation_command,
) -> dict[str, object]:
    result = executor(
        command, connector_cluster_id=cluster_id,
        allowed_namespaces=allowed_namespaces, observed_at=observed_at,
    )
    observation = result.get("observation")
    stdout = _json(observation) if isinstance(observation, dict) else ""
    if len(stdout.encode()) > 1024 * 1024:
        raise ValueError("Kubernetes reconciliation result exceeds output limit")
    status = str(result.get("status") or "failed")
    return {
        "status": status if status in {"succeeded", "failed", "rejected"} else "failed",
        "stdout": stdout, "stderr": "", "exit_code": 0 if status == "succeeded" else None,
        "truncated": False, "error_code": result.get("error_code"),
        "error_message": str(result.get("error_message"))[:500]
        if result.get("error_message") else None,
    }


def execute_mutation_command(
    command: dict[str, object], *, connector_id: str, cluster_id: str, allowed_namespaces: set[str],
    clock: Callable[[], float], executor: Callable[..., object],
) -> dict[str, object]:
    phases = []
    action = command.get("action")
    parameters = command.get("parameters")
    assert isinstance(parameters, dict)
    for phase, envelope in zip(
        ("preflight", "execution", "post_check"), build_mutation_envelopes(command, now=clock()), strict=True
    ):
        if phase == "execution" and clock() >= float(command["execution_grant_expires_at"]):
            return _rejected_result("execution_grant_expired", phases)
        result = executor(
            envelope, connector_id=connector_id, connector_cluster_id=cluster_id,
            allowed_namespaces=allowed_namespaces,
        ).to_dict()
        if result["status"] == "command_rejected":
            result["status"] = "rejected"
        if result["status"] == "succeeded" and action == "scale_deployment":
            expected = parameters["current_replicas"] if phase == "preflight" else parameters["target_replicas"]
            if phase != "execution" and deployment_replicas(result.get("stdout")) != expected:
                result["status"] = "failed"
                result["error_code"] = "target_changed" if phase == "preflight" else "post_check_failed"
        phases.append({"phase": phase, "status": result["status"], "error_code": result.get("error_code")})
        if result["status"] != "succeeded":
            if phase == "post_check":
                rollback = _execute_conditional_rollback(
                    command, connector_id=connector_id, cluster_id=cluster_id,
                    allowed_namespaces=allowed_namespaces, phases=phases, clock=clock, executor=executor,
                )
                if rollback is not None:
                    return rollback
                result["error_code"] = "rollback_required"
            result["stdout"] = _json({"phases": phases})
            result["error_code"] = result.get("error_code") or f"{phase}_failed"
            return {key: result.get(key) for key in (
                "status", "stdout", "stderr", "exit_code", "truncated", "error_code", "error_message"
            )}
    return {
        "status": "succeeded", "stdout": _json({"phases": phases}), "stderr": "", "exit_code": 0,
        "truncated": False, "error_code": None, "error_message": None,
    }


def _execute_conditional_rollback(
    command: dict[str, object], *, connector_id: str, cluster_id: str,
    allowed_namespaces: set[str], phases: list[dict[str, object]],
    clock: Callable[[], float], executor: Callable[..., object],
) -> dict[str, object] | None:
    plan = command.get("rollback_plan")
    parameters = command.get("parameters")
    if not isinstance(plan, dict) or plan.get("condition") != "post_check_failed" or not isinstance(parameters, dict):
        return None
    rollback = {
        **command,
        "action": plan.get("action_type"),
        "parameters": {
            "resource_kind": parameters.get("resource_kind"),
            "deployment_name": parameters.get("deployment_name"),
            **plan["parameters"],
        },
        "rollback_plan": None,
    }
    try:
        envelopes = build_mutation_envelopes(
            rollback, now=clock(), validate_frozen=False, validate_expiry=False
        )
    except (TypeError, ValueError, KeyError):
        return None
    assumptions = plan.get("target_assumptions")
    for phase, envelope in zip(("rollback_assumption", "rollback", "rollback_post_check"), envelopes, strict=True):
        result = executor(
            envelope, connector_id=connector_id, connector_cluster_id=cluster_id,
            allowed_namespaces=allowed_namespaces,
        ).to_dict()
        if result["status"] == "command_rejected":
            result["status"] = "rejected"
        expected = assumptions.get("replicas") if phase == "rollback_assumption" and isinstance(assumptions, dict) else (
            plan["parameters"].get("target_replicas") if phase == "rollback_post_check" else None
        )
        if result["status"] == "succeeded" and expected is not None and deployment_replicas(result.get("stdout")) != expected:
            result["status"] = "failed"
            result["error_code"] = "target_changed"
        phases.append({"phase": phase, "status": result["status"], "error_code": result.get("error_code")})
        if result["status"] != "succeeded":
            return _terminal_result("rollback_required", phases)
    return _terminal_result("rolled_back", phases)


def _terminal_result(error_code: str, phases: list[dict[str, object]]) -> dict[str, object]:
    return {
        "status": "failed", "stdout": _json({"phases": phases}), "stderr": "", "exit_code": 1,
        "truncated": False, "error_code": error_code, "error_message": error_code,
    }


def _rejected_result(error_code: str, phases: list[dict[str, object]]) -> dict[str, object]:
    return {
        "status": "rejected", "stdout": _json({"phases": phases}), "stderr": "", "exit_code": None,
        "truncated": False, "error_code": error_code, "error_message": error_code,
    }


def run_command_cycle(
    gateway_url: str,
    *,
    connector_id: str,
    cluster_id: str,
    credential: str,
    allowed_namespaces: set[str],
    journal: ConnectorCommandJournal,
    wait_seconds: float = 20.0,
    allow_insecure: bool = False,
    clock: Callable[[], float] = time.time,
    mutation_executor: Callable[..., object] = execute_command_envelope,
    validation_executor: Callable[..., dict[str, object]] = execute_validation_command,
    change_executor: Callable[..., dict[str, object]] = execute_change_command,
    reconciliation_executor: Callable[..., dict[str, object]] = execute_reconciliation_command,
) -> bool:
    if not connector_gateway_url_is_secure(gateway_url, allow_insecure=allow_insecure) or not credential:
        return False
    for command, result, evidence in journal.unreported_results_with_evidence():
        if _submit_result(
            gateway_url, connector_id, cluster_id, credential, command, result, evidence,
        ):
            journal.acknowledged(str(command["id"]))
    status, response = _post_json(
        gateway_url,
        "/api/v1/connectors/commands/poll",
        {"connector_id": connector_id, "cluster_id": cluster_id, "wait_seconds": wait_seconds},
        credential,
        timeout=wait_seconds + 5,
    )
    command = response.get("command") if status == 200 else None
    if not isinstance(command, dict):
        return False
    disposition = journal.accept(command)
    if disposition == "already_started":
        return False
    command_id = str(command["id"])
    start_status, _ = _post_json(
        gateway_url,
        f"/api/v1/connectors/commands/{command_id}/start",
        {
            "connector_id": connector_id,
            "cluster_id": cluster_id,
            "lease_id": command["lease_id"],
        },
        credential,
    )
    if start_status != 200:
        journal.redact_accepted(command_id)
        return False
    pending = journal.unreported_result(command_id)
    if pending is None:
        journal.started(command_id)
        try:
            if disposition == "duplicate_grant":
                pending = _rejected_result("execution_grant_reused", [])
            elif command.get("action") == "validate_kubernetes_change":
                pending = execute_kubernetes_validation(
                    command, cluster_id=cluster_id,
                    allowed_namespaces=allowed_namespaces, executor=validation_executor,
                )
            elif command.get("action") == "execute_kubernetes_change":
                parameters = command.get("parameters")
                change = parameters.get("change") if isinstance(parameters, dict) else None
                target = change.get("target") if isinstance(change, dict) else None
                scope = "/".join(str(item) for item in (
                    cluster_id, command.get("namespace"),
                    target.get("api_version") if isinstance(target, dict) else None,
                    target.get("kind") if isinstance(target, dict) else None,
                    target.get("name") if isinstance(target, dict) else None,
                ))
                if not journal.acquire_execution_lock(scope, command_id):
                    raise ValueError("Kubernetes target already has an active mutation")
                try:
                    pending = execute_kubernetes_change(
                        command, cluster_id=cluster_id, allowed_namespaces=allowed_namespaces,
                        now=clock(), clock=clock, executor=change_executor,
                    )
                finally:
                    journal.release_execution_lock(scope, command_id)
            elif command.get("action") == "reconcile_kubernetes_change":
                pending = execute_kubernetes_reconciliation(
                    command, cluster_id=cluster_id,
                    allowed_namespaces=allowed_namespaces, observed_at=clock(),
                    executor=reconciliation_executor,
                )
            elif command.get("action") != "get_resource":
                parameters = command.get("parameters")
                deployment = parameters.get("deployment_name") if isinstance(parameters, dict) else None
                scope = f"{cluster_id}/{command.get('namespace')}/Deployment/{deployment}"
                if not journal.acquire_execution_lock(scope, command_id):
                    raise ValueError("Deployment already has an active mutation")
                try:
                    pending = execute_mutation_command(
                        command, connector_id=connector_id, cluster_id=cluster_id,
                        allowed_namespaces=allowed_namespaces, clock=clock, executor=mutation_executor,
                    )
                finally:
                    journal.release_execution_lock(scope, command_id)
            else:
                pending = execute_read_command(
                    command, connector_id=connector_id, cluster_id=cluster_id,
                    allowed_namespaces=allowed_namespaces,
                )
        except (TypeError, ValueError) as exc:
            pending = {
                "status": "rejected", "stdout": "", "stderr": "", "exit_code": None,
                "truncated": False, "error_code": "command_rejected", "error_message": str(exc),
            }
        journal.terminal(command_id, pending)
    evidence = next(
        (
            item_evidence
            for item_command, _item_result, item_evidence in journal.unreported_results_with_evidence()
            if item_command.get("id") == command_id
        ),
        None,
    )
    if evidence is not None and _submit_result(
        gateway_url, connector_id, cluster_id, credential, command, pending, evidence,
    ):
        journal.acknowledged(command_id)
    return True


def _submit_result(
    gateway_url: str,
    connector_id: str,
    cluster_id: str,
    credential: str,
    command: dict[str, object],
    result: dict[str, object],
    journal_evidence: dict[str, object],
) -> bool:
    status, _ = _post_json(
        gateway_url,
        f"/api/v1/connectors/commands/{command['id']}/result",
        {
            "connector_id": connector_id,
            "cluster_id": cluster_id,
            "lease_id": command["lease_id"],
            "result": result,
            "journal_evidence": journal_evidence,
        },
        credential,
    )
    return status == 200


def _post_json(
    gateway_url: str,
    path: str,
    payload: dict[str, object],
    credential: str,
    *,
    timeout: float = 5.0,
) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        f"{gateway_url.rstrip('/')}{path}",
        data=_json(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {credential}",
            **request_context_headers(payload),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except (ValueError, json.JSONDecodeError):
            return exc.code, {}
    except (OSError, TimeoutError, urllib.error.URLError, ValueError, json.JSONDecodeError):
        return 0, {}


def _redact_secure_input_transport(command: object) -> object:
    if not isinstance(command, dict):
        return command
    parameters = command.get("parameters")
    refs = parameters.get("secure_inputs") if isinstance(parameters, dict) else None
    if not isinstance(refs, list):
        return command
    parameters["secure_inputs"] = public_secure_input_facts(refs)
    return command


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
