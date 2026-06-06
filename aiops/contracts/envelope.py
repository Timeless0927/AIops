"""Common MCP response envelope contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import ToolError
from .evidence import EvidenceRef


@dataclass(frozen=True)
class ToolEnvelope:
    """V1 common response envelope returned by MCP facades."""

    request_id: str
    tool_name: str
    status: str
    summary: str
    correlation_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[EvidenceRef, ...] = ()
    audit: dict[str, Any] = field(default_factory=dict)
    truncated: bool = False
    next_cursor: str | None = None
    errors: tuple[ToolError, ...] = ()
