"""CommandTask domain model."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
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


TERMINAL_STATUSES = {
    CommandTaskStatus.SUCCEEDED,
    CommandTaskStatus.FAILED,
    CommandTaskStatus.CANCELLED,
    CommandTaskStatus.EXPIRED,
}

ALLOWED_TRANSITIONS: dict[CommandTaskStatus, set[CommandTaskStatus]] = {
    CommandTaskStatus.CREATED: {
        CommandTaskStatus.PENDING_APPROVAL,
        CommandTaskStatus.APPROVED,
        CommandTaskStatus.QUEUED,
        CommandTaskStatus.CANCELLED,
        CommandTaskStatus.EXPIRED,
    },
    CommandTaskStatus.PENDING_APPROVAL: {
        CommandTaskStatus.APPROVED,
        CommandTaskStatus.CANCELLED,
        CommandTaskStatus.EXPIRED,
    },
    CommandTaskStatus.APPROVED: {
        CommandTaskStatus.QUEUED,
        CommandTaskStatus.DISPATCHED,
        CommandTaskStatus.CANCELLED,
        CommandTaskStatus.EXPIRED,
    },
    CommandTaskStatus.QUEUED: {
        CommandTaskStatus.DISPATCHED,
        CommandTaskStatus.CANCELLED,
        CommandTaskStatus.EXPIRED,
    },
    CommandTaskStatus.DISPATCHED: {
        CommandTaskStatus.RUNNING,
        CommandTaskStatus.SUCCEEDED,
        CommandTaskStatus.FAILED,
        CommandTaskStatus.CANCELLED,
        CommandTaskStatus.EXPIRED,
    },
    CommandTaskStatus.RUNNING: {
        CommandTaskStatus.SUCCEEDED,
        CommandTaskStatus.FAILED,
        CommandTaskStatus.CANCELLED,
        CommandTaskStatus.EXPIRED,
    },
    CommandTaskStatus.SUCCEEDED: set(),
    CommandTaskStatus.FAILED: set(),
    CommandTaskStatus.CANCELLED: set(),
    CommandTaskStatus.EXPIRED: set(),
}


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class TaskEvent:
    """Append-only CommandTask transition record."""

    task_id: str
    from_status: CommandTaskStatus | None
    to_status: CommandTaskStatus
    occurred_at: datetime = field(default_factory=utc_now)
    reason: str | None = None


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
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        *,
        task_id: str,
        command_id: str,
        cluster_id: str,
        namespace: str,
        action_type: str,
        **kwargs: object,
    ) -> "CommandTask":
        return cls(
            task_id=task_id,
            command_id=command_id,
            cluster_id=cluster_id,
            namespace=namespace,
            action_type=action_type,
            status=CommandTaskStatus.CREATED,
            **kwargs,
        )

    def transition_to(
        self,
        status: CommandTaskStatus,
        *,
        reason: str | None = None,
        occurred_at: datetime | None = None,
    ) -> tuple["CommandTask", TaskEvent]:
        if self.status in TERMINAL_STATUSES:
            raise ValueError(f"cannot transition terminal task from {self.status.value}")
        if status not in ALLOWED_TRANSITIONS[self.status]:
            raise ValueError(f"invalid task transition {self.status.value}->{status.value}")

        timestamp = occurred_at or utc_now()
        event = TaskEvent(
            task_id=self.task_id,
            from_status=self.status,
            to_status=status,
            occurred_at=timestamp,
            reason=reason,
        )
        return replace(self, status=status, reason=reason, updated_at=timestamp), event


class CommandTaskStore:
    """Small in-memory store for command_id idempotency and local tests."""

    def __init__(self) -> None:
        self._tasks_by_id: dict[str, CommandTask] = {}
        self._task_id_by_command_id: dict[str, str] = {}
        self._events: list[TaskEvent] = []

    def create(self, task: CommandTask) -> CommandTask:
        existing_task_id = self._task_id_by_command_id.get(task.command_id)
        if existing_task_id is not None:
            return self._tasks_by_id[existing_task_id]

        self._tasks_by_id[task.task_id] = task
        self._task_id_by_command_id[task.command_id] = task.task_id
        self._events.append(TaskEvent(task.task_id, None, task.status, task.created_at, "created"))
        return task

    def get(self, task_id: str) -> CommandTask | None:
        return self._tasks_by_id.get(task_id)

    def transition(
        self,
        task_id: str,
        status: CommandTaskStatus,
        *,
        reason: str | None = None,
    ) -> CommandTask:
        task = self._tasks_by_id[task_id]
        updated, event = task.transition_to(status, reason=reason)
        self._tasks_by_id[task_id] = updated
        self._events.append(event)
        return updated

    def events_for(self, task_id: str) -> tuple[TaskEvent, ...]:
        return tuple(event for event in self._events if event.task_id == task_id)
