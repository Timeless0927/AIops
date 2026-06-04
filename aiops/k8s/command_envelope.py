"""Gateway-to-Connector command envelope."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CommandEnvelope:
    """Command envelope dispatched from Gateway to Connector."""

    envelope_version: str
    task_id: str
    command_id: str
    cluster_id: str
    namespace: str
    action_type: str
    argv: tuple[str, ...]
    timeout_seconds: int
    output_limit_bytes: int
    risk_level: str | None = None
    grant_id: str | None = None
    reason: str | None = None
