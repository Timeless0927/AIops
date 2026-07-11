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
        "lease_id", "lease_expires_at", "created_at", "result"
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
            pending = execute_read_command(
                command,
                connector_id=connector_id,
                cluster_id=cluster_id,
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
