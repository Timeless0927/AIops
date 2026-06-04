"""Service identity domain model."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ServiceIdentity:
    """V1 service key: cluster_id + namespace + service."""

    cluster_id: str
    namespace: str
    service: str

    @property
    def service_key(self) -> str:
        """Return the human-readable service key."""
        return f"{self.cluster_id}/{self.namespace}/{self.service}"
