"""Shared immutable value types for Acceptance Evidence Module consumers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


GateStatus = Literal["passed", "failed", "not_applicable"]


@dataclass(frozen=True)
class Artifact:
    path: Path
    relative_path: str
    sha256: str
    size: int
    gate_id: str
    attempt: int = 1


@dataclass(frozen=True)
class GateAttempt:
    gate_id: str
    attempt: int
    status: GateStatus
    started_at: str
    completed_at: str
    artifacts: tuple[Artifact, ...]
    failure_attribution: str | None = None


@dataclass(frozen=True)
class GateExecution:
    gate_id: str
    execution_id: str
    started_at: str
    operations: tuple[dict[str, str], ...]
    artifacts: tuple[Artifact, ...]
    reconciliations: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class GateResult:
    status: GateStatus
    artifacts: tuple[Artifact, ...]
    failure_attribution: str | None = None
