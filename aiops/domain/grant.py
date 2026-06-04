"""Execution grant domain model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Grant:
    """One-time execution authorization issued by Gateway."""

    grant_id: str
    task_id: str
    command_id: str
    cluster_id: str
    namespace: str
    action: str
    risk_level: str
    issued_by: str
    issued_at: datetime
    expires_at: datetime
    max_uses: int = 1
    approval_id: str | None = None
    signature: str | None = None
