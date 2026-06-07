"""Entry point for the Prometheus MCP process."""

from __future__ import annotations

from apps.observability_http import run_service

from . import APP_NAME
from .facade import TOOL_NAME, query_metrics


def main() -> None:
    """Start the Prometheus MCP HTTP service."""
    run_service(
        service_name=APP_NAME,
        tool_name=TOOL_NAME,
        query_path="/query_metrics",
        query_handler=query_metrics,
        description="Prometheus MCP query_metrics service",
        env_host="AIOPS_PROMETHEUS_MCP_HOST",
        env_port="AIOPS_PROMETHEUS_MCP_PORT",
        default_port=8083,
    )


if __name__ == "__main__":
    main()
