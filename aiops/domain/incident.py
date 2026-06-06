"""Incident diagnosis domain model."""

from __future__ import annotations

from dataclasses import dataclass, field

from aiops.contracts import EvidenceRef

from .service_identity import ServiceIdentity


@dataclass(frozen=True)
class IncidentRecord:
    """Auditable incident diagnosis record."""

    incident_id: str
    service: ServiceIdentity
    status: str
    summary: str
    evidence_refs: tuple[EvidenceRef, ...] = ()
    confidence: float | None = None
    lessons: tuple[str, ...] = field(default_factory=tuple)
