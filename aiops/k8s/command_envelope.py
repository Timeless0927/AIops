"""Gateway-to-Connector command envelope."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


SUPPORTED_ENVELOPE_VERSION = "v1"


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

    def __post_init__(self) -> None:
        if isinstance(self.argv, list):
            object.__setattr__(self, "argv", tuple(self.argv))
        self.validate()

    def validate(self) -> None:
        required = {
            "envelope_version": self.envelope_version,
            "task_id": self.task_id,
            "command_id": self.command_id,
            "cluster_id": self.cluster_id,
            "namespace": self.namespace,
            "action_type": self.action_type,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(f"missing command envelope field: {', '.join(missing)}")
        if self.envelope_version != SUPPORTED_ENVELOPE_VERSION:
            raise ValueError("unsupported command envelope version")
        if not self.argv or any(not isinstance(item, str) or not item for item in self.argv):
            raise ValueError("argv must be a non-empty string array")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.output_limit_bytes <= 0:
            raise ValueError("output_limit_bytes must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "envelope_version": self.envelope_version,
            "task_id": self.task_id,
            "command_id": self.command_id,
            "cluster_id": self.cluster_id,
            "namespace": self.namespace,
            "action_type": self.action_type,
            "argv": list(self.argv),
            "timeout_seconds": self.timeout_seconds,
            "output_limit_bytes": self.output_limit_bytes,
            "risk_level": self.risk_level,
            "grant_id": self.grant_id,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CommandEnvelope":
        return cls(
            envelope_version=payload.get("envelope_version", ""),
            task_id=payload.get("task_id", ""),
            command_id=payload.get("command_id", ""),
            cluster_id=payload.get("cluster_id", ""),
            namespace=payload.get("namespace", ""),
            action_type=payload.get("action_type", ""),
            argv=tuple(payload.get("argv", ())),
            timeout_seconds=int(payload.get("timeout_seconds", 0)),
            output_limit_bytes=int(payload.get("output_limit_bytes", 0)),
            risk_level=payload.get("risk_level"),
            grant_id=payload.get("grant_id"),
            reason=payload.get("reason"),
        )
