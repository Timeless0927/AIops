"""CommandTask orchestration boundary for the K8s Gateway."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from typing import Any

from aiops.domain import CommandTask
from aiops.k8s import CommandEnvelope, ResultEnvelope

from .connector_router import ConnectorRoute


def describe_task(task: CommandTask) -> str:
    """Return a stable human-readable task summary for logs and smoke checks."""
    return f"{task.task_id}:{task.action_type}:{task.status}"


def build_read_envelope(payload: dict[str, Any]) -> CommandEnvelope:
    """Build a read-only command envelope from a Gateway request payload."""
    task_id = str(payload.get("task_id") or f"task-read-{uuid.uuid4().hex}")
    command_id = str(payload.get("command_id") or f"cmd-read-{uuid.uuid4().hex}")
    argv = payload.get("argv")
    if isinstance(argv, str):
        raise ValueError("argv must be an array, not a shell string")
    return CommandEnvelope(
        envelope_version="v1",
        task_id=task_id,
        command_id=command_id,
        cluster_id=str(payload.get("cluster_id") or ""),
        namespace=str(payload.get("namespace") or ""),
        action_type="read",
        argv=tuple(argv or ()),
        timeout_seconds=int(payload.get("timeout_seconds") or 15),
        output_limit_bytes=int(payload.get("output_limit_bytes") or 262144),
        risk_level="low",
        grant_id=str(payload.get("grant_id") or f"read-{command_id}"),
        reason=payload.get("reason"),
    )


def dispatch_read_envelope(
    envelope: CommandEnvelope,
    *,
    route: ConnectorRoute,
    connector_url: str,
    timeout: float = 5.0,
) -> ResultEnvelope:
    """Dispatch a command envelope to the registered Connector HTTP surface."""
    if envelope.cluster_id != route.cluster_id:
        raise ValueError("cluster_id does not match registered connector")
    body = json.dumps(envelope.to_dict()).encode("utf-8")
    request = urllib.request.Request(
        f"{connector_url.rstrip('/')}/commands/execute",
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read().decode("utf-8") or "{}")
    except (OSError, TimeoutError, urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
        return ResultEnvelope(
            envelope_version="v1",
            task_id=envelope.task_id,
            command_id=envelope.command_id,
            connector_id=route.connector_id,
            cluster_id=route.cluster_id,
            status="failed",
            error_code="connector_offline",
            error_message=f"connector dispatch failed: {exc}",
        )
    return ResultEnvelope.from_dict(payload)
