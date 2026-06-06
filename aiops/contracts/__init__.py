"""Shared contracts for MCP tools, evidence, errors, and time ranges."""

from .envelope import ToolEnvelope
from .errors import ErrorCode, ToolError
from .evidence import EvidenceRef
from .time_range import TimeRange

__all__ = [
    "ErrorCode",
    "EvidenceRef",
    "TimeRange",
    "ToolEnvelope",
    "ToolError",
]
