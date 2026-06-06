"""Loki MCP facade boundary."""

from __future__ import annotations

from aiops.contracts import ToolEnvelope
from toolsets.loki_query import LokiRunner, query_logs as _query_logs


TOOL_NAME = "query_logs"


def tool_name() -> str:
    """Return the V1 Loki MCP tool name."""
    return TOOL_NAME


def empty_response(request_id: str, correlation_id: str | None = None) -> ToolEnvelope:
    """Build a placeholder response envelope for contract-level tests."""
    return ToolEnvelope(
        request_id=request_id,
        correlation_id=correlation_id,
        tool_name=TOOL_NAME,
        status="partial",
        summary="Loki MCP facade is not implemented yet",
    )


async def query_logs(args: dict, runner: LokiRunner | None = None) -> ToolEnvelope:
    """Run the Loki query_logs facade."""
    return await _query_logs(args, runner=runner)
