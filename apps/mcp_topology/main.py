"""Entry point for the Topology MCP process."""

from __future__ import annotations

from aiops.contracts import ToolEnvelope
from apps.observability_http import run_service

from . import APP_NAME
from .facade import TOOL_NAME, get_service_topology


async def query_service_topology(args: dict) -> ToolEnvelope:
    """Run the synchronous topology facade behind the shared HTTP runtime."""
    return get_service_topology(args)


def main() -> None:
    """Start the Topology MCP HTTP service."""
    run_service(
        service_name=APP_NAME,
        tool_name=TOOL_NAME,
        query_path="/get_service_topology",
        query_handler=query_service_topology,
        description="Topology MCP get_service_topology service",
        env_host="AIOPS_TOPOLOGY_MCP_HOST",
        env_port="AIOPS_TOPOLOGY_MCP_PORT",
        default_port=8085,
    )


if __name__ == "__main__":
    main()
