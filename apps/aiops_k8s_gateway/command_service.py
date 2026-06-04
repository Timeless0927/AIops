"""CommandTask orchestration boundary for the K8s Gateway."""

from __future__ import annotations

from aiops.domain import CommandTask


def describe_task(task: CommandTask) -> str:
    """Return a stable human-readable task summary for logs and smoke checks."""
    return f"{task.task_id}:{task.action_type}:{task.status}"
