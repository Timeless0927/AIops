"""Execution grant domain model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


def _ensure_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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
    uses: int = 0

    def __post_init__(self) -> None:
        if self.max_uses != 1:
            raise ValueError("AIO-57 grants are one-time use only")
        if self.expires_at <= self.issued_at:
            raise ValueError("grant expires_at must be after issued_at")

    def is_expired(self, now: datetime | None = None) -> bool:
        current = _ensure_aware_utc(now or datetime.now(timezone.utc))
        return current >= _ensure_aware_utc(self.expires_at)

    def can_use(self, now: datetime | None = None) -> bool:
        return not self.is_expired(now) and self.uses < self.max_uses

    def consume(self, now: datetime | None = None) -> "Grant":
        if not self.can_use(now):
            raise ValueError("grant is expired or already consumed")
        return Grant(
            grant_id=self.grant_id,
            task_id=self.task_id,
            command_id=self.command_id,
            cluster_id=self.cluster_id,
            namespace=self.namespace,
            action=self.action,
            risk_level=self.risk_level,
            issued_by=self.issued_by,
            issued_at=self.issued_at,
            expires_at=self.expires_at,
            max_uses=self.max_uses,
            approval_id=self.approval_id,
            signature=self.signature,
            uses=self.uses + 1,
        )
