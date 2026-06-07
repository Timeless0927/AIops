"""Entry point for the Loki MCP process."""

from __future__ import annotations

from apps.observability_http import run_service

from . import APP_NAME
from .facade import TOOL_NAME, query_logs


def main() -> None:
    """Start the Loki MCP HTTP service."""
    run_service(
        service_name=APP_NAME,
        tool_name=TOOL_NAME,
        query_path="/query_logs",
        query_handler=query_logs,
        description="Loki MCP query_logs service",
        env_host="AIOPS_LOKI_MCP_HOST",
        env_port="AIOPS_LOKI_MCP_PORT",
        default_port=8084,
    )


if __name__ == "__main__":
    main()
