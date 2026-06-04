"""Shared V1 tool error contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    """Standard error codes shared by V1 MCP and Gateway tools."""

    INVALID_REQUEST = "invalid_request"
    UNAUTHORIZED = "unauthorized"
    CLUSTER_NOT_FOUND = "cluster_not_found"
    NAMESPACE_NOT_FOUND = "namespace_not_found"
    SERVICE_NOT_FOUND = "service_not_found"
    QUERY_REJECTED = "query_rejected"
    QUERY_COST_EXCEEDED = "query_cost_exceeded"
    TIMEOUT = "timeout"
    OUTPUT_TRUNCATED = "output_truncated"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    CONNECTOR_OFFLINE = "connector_offline"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_DENIED = "approval_denied"
    EXECUTION_FAILED = "execution_failed"
    TASK_NOT_FOUND = "task_not_found"


@dataclass(frozen=True)
class ToolError:
    """Structured tool error."""

    code: ErrorCode
    message: str
    details: dict[str, Any] = field(default_factory=dict)
