"""Process-local readiness fault state for the controlled verification fixture."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from enum import StrEnum


_RUN_ID = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,62}[a-z0-9])?$")


class TriggerOutcome(StrEnum):
    ACTIVATED = "activated"
    REPLAYED = "replayed"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class FaultSnapshot:
    run_id: str
    active: bool


class FaultLatch:
    """Latch one Kubernetes-generated run until a Deployment rollout replaces the process."""

    def __init__(self, recovery_run_id: str = "") -> None:
        if recovery_run_id:
            validate_run_id(recovery_run_id)
        self._snapshot = FaultSnapshot(run_id=recovery_run_id, active=False)
        self._lock = threading.Lock()

    def trigger(self, run_id: str) -> TriggerOutcome:
        validate_run_id(run_id)
        with self._lock:
            if not self._snapshot.active:
                self._snapshot = FaultSnapshot(run_id=run_id, active=True)
                return TriggerOutcome.ACTIVATED
            if self._snapshot.run_id == run_id:
                return TriggerOutcome.REPLAYED
            return TriggerOutcome.CONFLICT

    def snapshot(self) -> FaultSnapshot:
        with self._lock:
            return self._snapshot


def validate_run_id(run_id: str) -> None:
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError("run_id must be 1-64 lowercase DNS-safe characters")
