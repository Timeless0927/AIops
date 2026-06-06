"""Connector-to-Gateway result envelope."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


SUPPORTED_ENVELOPE_VERSION = "v1"
VALID_RESULT_STATUSES = {"succeeded", "failed", "cancelled", "expired", "command_rejected"}


@dataclass(frozen=True)
class ResultEnvelope:
    """Execution result envelope returned by Connector."""

    envelope_version: str
    task_id: str
    command_id: str
    connector_id: str
    cluster_id: str
    status: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    truncated: bool = False
    result_ref: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        required = {
            "envelope_version": self.envelope_version,
            "task_id": self.task_id,
            "command_id": self.command_id,
            "connector_id": self.connector_id,
            "cluster_id": self.cluster_id,
            "status": self.status,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(f"missing result envelope field: {', '.join(missing)}")
        if self.envelope_version != SUPPORTED_ENVELOPE_VERSION:
            raise ValueError("unsupported result envelope version")
        if self.status not in VALID_RESULT_STATUSES:
            raise ValueError("invalid result status")
        if self.exit_code is not None and not isinstance(self.exit_code, int):
            raise ValueError("exit_code must be an integer when present")

    def to_dict(self) -> dict[str, Any]:
        return {
            "envelope_version": self.envelope_version,
            "task_id": self.task_id,
            "command_id": self.command_id,
            "connector_id": self.connector_id,
            "cluster_id": self.cluster_id,
            "status": self.status,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "truncated": self.truncated,
            "result_ref": self.result_ref,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ResultEnvelope":
        return cls(
            envelope_version=payload.get("envelope_version", ""),
            task_id=payload.get("task_id", ""),
            command_id=payload.get("command_id", ""),
            connector_id=payload.get("connector_id", ""),
            cluster_id=payload.get("cluster_id", ""),
            status=payload.get("status", ""),
            stdout=payload.get("stdout", ""),
            stderr=payload.get("stderr", ""),
            exit_code=payload.get("exit_code"),
            truncated=bool(payload.get("truncated", False)),
            result_ref=payload.get("result_ref"),
            error_code=payload.get("error_code"),
            error_message=payload.get("error_message"),
        )
