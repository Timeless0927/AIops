"""Connector-to-Gateway result envelope."""

from __future__ import annotations

from dataclasses import dataclass


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
