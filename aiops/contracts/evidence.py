"""Evidence reference contracts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EvidenceRef:
    """Opaque evidence handle returned by observability and Gateway tools."""

    ref_id: str
    source: str
    cluster_id: str
    namespace: str | None = None
    service: str | None = None
    time_range: str | None = None
    query_digest: str | None = None
    cursor: str | None = None
