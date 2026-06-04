"""CommandTask domain model."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CommandTaskStatus(str, Enum):
    """V1 CommandTask states."""

    CREATED = "created"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    QUEUED = "queued"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass(frozen=True)
class CommandTask:
    """Gateway-owned task state for K8s read and mutation actions."""

    task_id: str
    command_id: str
    cluster_id: str
    namespace: str
    action_type: str
    status: CommandTaskStatus
    resource_kind: str | None = None
    resource_name: str | None = None
    risk_level: str | None = None
    requires_approval: bool = False
    approval_id: str | None = None
    grant_id: str | None = None
    connector_id: str | None = None
    reason: str | None = None
