"""Connector Command owner Interface for read-like Kubernetes validation."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

from aiops.contracts import validate_draft_kubernetes_change

from . import connector_command_schema as _schema  # noqa: F401


class ConnectorValidationCommands:
    def __init__(self, *, id_factory: Callable[[str], str] | None = None) -> None:
        self._id_factory = id_factory or (lambda prefix: f"{prefix}-{uuid.uuid4().hex}")

    def queue_in(
        self,
        conn: Any,
        *,
        connector_id: str,
        cluster_id: str,
        change: object,
        now: float,
    ) -> str:
        normalized = validate_draft_kubernetes_change(change)
        target = normalized["target"]
        assert isinstance(target, dict)
        command_id = self._id_factory("command")
        conn.execute(
            """
            INSERT INTO connector_commands (
                id, connector_id, cluster_id, namespace, action, parameters_json,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'validate_kubernetes_change', ?, 'queued', ?, ?)
            """,
            (
                command_id,
                _required_text(connector_id, "connector_id"),
                _required_text(cluster_id, "cluster_id"),
                target["namespace"] or "",
                _json({"change": normalized}),
                now,
                now,
            ),
        )
        return command_id


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError(f"{field} is required")
    return value.strip()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

