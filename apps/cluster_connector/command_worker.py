"""Connector-owned durable read command worker and local journal."""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from aiops.k8s import CommandEnvelope

from .gateway_client import connector_gateway_url_is_secure
from .kubectl_executor import execute_command_envelope


_RESOURCE_KINDS = {"pods", "deployments", "services", "events"}
_OUTPUTS = {"json", "yaml", "wide"}


class ConnectorCommandJournal:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS command_journal (
                    command_id TEXT PRIMARY KEY,
                    command_json TEXT NOT NULL CHECK (json_valid(command_json)),
                    state TEXT NOT NULL CHECK (state IN ('accepted', 'started', 'terminal', 'acknowledged')),
                    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS execution_locks (
                    scope TEXT PRIMARY KEY,
                    command_id TEXT NOT NULL UNIQUE,
                    acquired_at REAL NOT NULL
                )
                """
            )

    def accept(self, command: dict[str, object]) -> None:
        command_id = str(command.get("id") or "")
        if not command_id:
            raise ValueError("Connector Command id is required")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO command_journal (command_id, command_json, state, updated_at)
                VALUES (?, ?, 'accepted', ?)
                ON CONFLICT(command_id) DO UPDATE SET command_json = excluded.command_json, updated_at = excluded.updated_at
                WHERE command_journal.state IN ('accepted', 'started')
                """,
                (command_id, _json(command), time.time()),
            )

    def started(self, command_id: str) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE command_journal SET state = 'started', updated_at = ? WHERE command_id = ? AND state IN ('accepted', 'started')",
                (time.time(), command_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Connector Command must be accepted before started")

    def terminal(self, command_id: str, result: dict[str, object]) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE command_journal SET state = 'terminal', result_json = ?, updated_at = ? WHERE command_id = ? AND state = 'started'",
                (_json(result), time.time(), command_id),
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

    def unreported_result(self, command_id: str) -> dict[str, object] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT result_json FROM command_journal WHERE command_id = ? AND state = 'terminal'", (command_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def acquire_execution_lock(self, scope: str, command_id: str) -> bool:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO execution_locks (scope, command_id, acquired_at) VALUES (?, ?, ?)",
                (scope, command_id, time.time()),
            )
            return conn.execute("SELECT command_id FROM execution_locks WHERE scope = ?", (scope,)).fetchone()[0] == command_id

    def release_execution_lock(self, scope: str, command_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM execution_locks WHERE scope = ? AND command_id = ?", (scope, command_id))

    def _transition(self, command_id: str, state: str, *, expected: str = "accepted") -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE command_journal SET state = ?, updated_at = ? WHERE command_id = ? AND state = ?",
                (state, time.time(), command_id, expected),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Connector Command must be {expected} before {state}")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.db_path))


def build_read_envelope(command: dict[str, object]) -> CommandEnvelope:
    if set(command) - {
        "id", "cluster_id", "namespace", "action", "parameters", "status", "attempt_count",
        "lease_id", "lease_expires_at", "created_at", "result", "execution_grant_id", "execution_grant_expires_at", "action_hash",
        "rollback_plan",
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


def build_restart_envelopes(command: dict[str, object]) -> tuple[CommandEnvelope, CommandEnvelope, CommandEnvelope]:
    if command.get("action") != "restart_deployment":
        raise ValueError("unsupported mutation action")
    return build_mutation_envelopes(command)


def build_mutation_envelopes(command: dict[str, object]) -> tuple[CommandEnvelope, CommandEnvelope, CommandEnvelope]:
    if set(command) - {
        "id", "cluster_id", "namespace", "action", "parameters", "status", "attempt_count",
        "lease_id", "lease_expires_at", "created_at", "result", "execution_grant_id", "execution_grant_expires_at", "action_hash",
        "rollback_plan",
    }:
        raise ValueError("unsupported Connector Command fields")
    parameters = command.get("parameters")
    action = command.get("action")
    expected = {
        "restart_deployment": {"resource_kind", "deployment_name"},
        "scale_deployment": {"resource_kind", "deployment_name", "current_replicas", "target_replicas"},
        "rollback_deployment": {"resource_kind", "deployment_name", "target_revision"},
    }
    if action not in expected or not isinstance(parameters, dict) or set(parameters) != expected[action]:
        raise ValueError("unsupported mutation action")
    name = parameters.get("deployment_name")
    grant_id = command.get("execution_grant_id")
    action_hash = command.get("action_hash")
    if parameters.get("resource_kind") != "Deployment" or not isinstance(name, str) or not name.strip():
        raise ValueError("invalid Deployment target")
    if not isinstance(grant_id, str) or not grant_id or not isinstance(action_hash, str) or len(action_hash) != 64:
        raise ValueError("Execution Grant and action hash are required")
    if not isinstance(command.get("execution_grant_expires_at"), (int, float)) or float(command["execution_grant_expires_at"]) <= time.time():
        raise ValueError("Execution Grant expired")
    command_id = str(command.get("id") or "")
    cluster_id = str(command.get("cluster_id") or "")
    namespace = str(command.get("namespace") or "")

    def envelope(suffix: str, action_type: str, argv: tuple[str, ...]) -> CommandEnvelope:
        return CommandEnvelope(
            envelope_version="v1", task_id=command_id, command_id=f"{command_id}:{suffix}",
            cluster_id=cluster_id, namespace=namespace, action_type=action_type, argv=argv,
            timeout_seconds=120, output_limit_bytes=1024 * 1024, risk_level="low",
            grant_id=grant_id, reason=action_hash,
        )

    target = f"deployment/{name.strip()}"
    namespace_args = ("--namespace", namespace)
    if action == "scale_deployment":
        current, desired = parameters["current_replicas"], parameters["target_replicas"]
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > 20 for value in (current, desired)):
            raise ValueError("replica bounds require values from 0 to 20")
        if current == desired:
            raise ValueError("scale target must differ from current replicas")
        execution = ("kubectl", "scale", target, "--replicas", str(desired), *namespace_args)
        post_check = ("kubectl", "get", target, *namespace_args, "--output", "json")
    elif action == "rollback_deployment":
        revision = parameters["target_revision"]
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise ValueError("explicit positive target revision is required")
        execution = ("kubectl", "rollout", "undo", target, "--to-revision", str(revision), *namespace_args)
        post_check = ("kubectl", "get", target, *namespace_args, "--output", "json")
    else:
        execution = ("kubectl", "rollout", "restart", target, *namespace_args)
        post_check = ("kubectl", "rollout", "status", target, *namespace_args)
    _validated_rollback_plan(action, parameters, command.get("rollback_plan"))
    return (
        envelope(
            "preflight", "read",
            ("kubectl", "rollout", "history", target, "--revision", str(parameters["target_revision"]), *namespace_args)
            if action == "rollback_deployment" else ("kubectl", "get", target, *namespace_args, "--output", "json"),
        ),
        envelope("execute", "mutation", execution),
        envelope("post-check", "read", post_check),
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


def execute_restart_command(
    command: dict[str, object], *, connector_id: str, cluster_id: str, allowed_namespaces: set[str]
) -> dict[str, object]:
    return execute_mutation_command(
        command, connector_id=connector_id, cluster_id=cluster_id, allowed_namespaces=allowed_namespaces
    )


def execute_mutation_command(
    command: dict[str, object], *, connector_id: str, cluster_id: str, allowed_namespaces: set[str]
) -> dict[str, object]:
    phases = []
    action = command.get("action")
    parameters = command.get("parameters")
    assert isinstance(parameters, dict)
    for phase, envelope in zip(("preflight", "execution", "post_check"), build_mutation_envelopes(command), strict=True):
        result = execute_command_envelope(
            envelope, connector_id=connector_id, connector_cluster_id=cluster_id,
            allowed_namespaces=allowed_namespaces,
        ).to_dict()
        if result["status"] == "command_rejected":
            result["status"] = "rejected"
        if result["status"] == "succeeded" and action == "scale_deployment":
            expected = parameters["current_replicas"] if phase == "preflight" else parameters["target_replicas"]
            if phase != "execution" and _deployment_replicas(result.get("stdout")) != expected:
                result["status"] = "failed"
                result["error_code"] = "target_changed" if phase == "preflight" else "post_check_failed"
        if (
            result["status"] == "succeeded" and action == "rollback_deployment" and phase == "post_check"
            and _deployment_revision(result.get("stdout")) != parameters["target_revision"]
        ):
            result["status"] = "failed"
            result["error_code"] = "post_check_failed"
        phases.append({"phase": phase, "status": result["status"], "error_code": result.get("error_code")})
        if result["status"] != "succeeded":
            if phase == "post_check":
                rollback = _execute_conditional_rollback(
                    command, connector_id=connector_id, cluster_id=cluster_id,
                    allowed_namespaces=allowed_namespaces, phases=phases,
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
        envelopes = build_mutation_envelopes(rollback)
    except (TypeError, ValueError, KeyError):
        return None
    assumptions = plan.get("target_assumptions")
    for phase, envelope in zip(("rollback_assumption", "rollback", "rollback_post_check"), envelopes, strict=True):
        result = execute_command_envelope(
            envelope, connector_id=connector_id, connector_cluster_id=cluster_id,
            allowed_namespaces=allowed_namespaces,
        ).to_dict()
        if result["status"] == "command_rejected":
            result["status"] = "rejected"
        expected = assumptions.get("replicas") if phase == "rollback_assumption" and isinstance(assumptions, dict) else (
            plan["parameters"].get("target_replicas") if phase == "rollback_post_check" else None
        )
        if result["status"] == "succeeded" and expected is not None and _deployment_replicas(result.get("stdout")) != expected:
            result["status"] = "failed"
            result["error_code"] = "target_changed"
        phases.append({"phase": phase, "status": result["status"], "error_code": result.get("error_code")})
        if result["status"] != "succeeded":
            return _terminal_result("rollback_required", phases)
    return _terminal_result("rolled_back", phases)


def _validated_rollback_plan(action: object, parameters: dict[str, object], plan: object) -> None:
    if plan is None or isinstance(plan, dict) and plan.get("type") == "none":
        return
    if not isinstance(plan, dict) or action != "scale_deployment" or set(plan) != {
        "condition", "action_type", "parameters", "target_assumptions"
    }:
        raise ValueError("unsafe conditional Rollback Plan")
    expected_parameters = {
        "current_replicas": parameters.get("target_replicas"),
        "target_replicas": parameters.get("current_replicas"),
    }
    if (
        plan.get("condition") != "post_check_failed"
        or plan.get("action_type") != "scale_deployment"
        or plan.get("parameters") != expected_parameters
        or plan.get("target_assumptions") != {"replicas": parameters.get("target_replicas")}
    ):
        raise ValueError("unsafe conditional Rollback Plan")


def _deployment_replicas(stdout: object) -> int | None:
    try:
        value = json.loads(stdout) if isinstance(stdout, str) else None
        replicas = value["spec"]["replicas"]
        return replicas if isinstance(replicas, int) and not isinstance(replicas, bool) else None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _deployment_revision(stdout: object) -> int | None:
    try:
        value = json.loads(stdout) if isinstance(stdout, str) else None
        revision = value["metadata"]["annotations"]["deployment.kubernetes.io/revision"]
        return int(revision) if isinstance(revision, (int, str)) and not isinstance(revision, bool) else None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _terminal_result(error_code: str, phases: list[dict[str, object]]) -> dict[str, object]:
    return {
        "status": "failed", "stdout": _json({"phases": phases}), "stderr": "", "exit_code": 1,
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
) -> bool:
    if not connector_gateway_url_is_secure(gateway_url, allow_insecure=allow_insecure) or not credential:
        return False
    for command, result in journal.unreported_results():
        if _submit_result(gateway_url, connector_id, cluster_id, credential, command, result):
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
    journal.accept(command)
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
        return False
    pending = journal.unreported_result(command_id)
    if pending is None:
        journal.started(command_id)
        try:
            if command.get("action") != "get_resource":
                parameters = command.get("parameters")
                deployment = parameters.get("deployment_name") if isinstance(parameters, dict) else None
                scope = f"{cluster_id}/{command.get('namespace')}/Deployment/{deployment}"
                if not journal.acquire_execution_lock(scope, command_id):
                    raise ValueError("Deployment already has an active mutation")
                try:
                    pending = execute_mutation_command(
                        command, connector_id=connector_id, cluster_id=cluster_id,
                        allowed_namespaces=allowed_namespaces,
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
    if _submit_result(gateway_url, connector_id, cluster_id, credential, command, pending):
        journal.acknowledged(command_id)
    return True


def _submit_result(
    gateway_url: str,
    connector_id: str,
    cluster_id: str,
    credential: str,
    command: dict[str, object],
    result: dict[str, object],
) -> bool:
    status, _ = _post_json(
        gateway_url,
        f"/api/v1/connectors/commands/{command['id']}/result",
        {
            "connector_id": connector_id,
            "cluster_id": cluster_id,
            "lease_id": command["lease_id"],
            "result": result,
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
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {credential}"},
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


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
