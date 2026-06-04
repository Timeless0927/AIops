"""Prometheus MCP facade boundary."""

from __future__ import annotations

from aiops.contracts import ToolEnvelope


TOOL_NAME = "query_metrics"


def tool_name() -> str:
    """Return the V1 Prometheus MCP tool name."""
    return TOOL_NAME


def empty_response(request_id: str, correlation_id: str | None = None) -> ToolEnvelope:
    """Build a placeholder response envelope for contract-level tests."""
    return ToolEnvelope(
        request_id=request_id,
        correlation_id=correlation_id,
        tool_name=TOOL_NAME,
        status="partial",
        summary="Prometheus MCP facade is not implemented yet",
    )
