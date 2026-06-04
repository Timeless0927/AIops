"""Topology domain models."""

from __future__ import annotations

from dataclasses import dataclass

from .service_identity import ServiceIdentity


@dataclass(frozen=True)
class ServiceNode:
    """Service catalog node."""

    identity: ServiceIdentity
    service_category: str = "unknown"
    workload_kind: str | None = None
    workload_name: str | None = None
    freshness: str | None = None


@dataclass(frozen=True)
class ServiceEdge:
    """Directed dependency edge between service nodes."""

    src: ServiceIdentity
    dst: ServiceIdentity
    edge_type: str
    source: str
    confidence: float
    freshness: str | None = None
