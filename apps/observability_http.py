"""HTTP runtime wrapper for observability MCP facades."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from http import HTTPStatus
from typing import Awaitable, Callable

from aiops.contracts import ErrorCode, ToolEnvelope, ToolError
from apps.service_http import JsonHandler, serve


QueryHandler = Callable[[dict], Awaitable[ToolEnvelope]]


def _failure_envelope(
    *,
    tool_name: str,
    request_id: str,
    correlation_id: str | None,
    message: str,
) -> ToolEnvelope:
    return ToolEnvelope(
        request_id=request_id or f"{tool_name}_http_error",
        correlation_id=correlation_id,
        tool_name=tool_name,
        status="failed",
        summary=f"{tool_name} 执行失败",
        data={},
        audit={
            "tool_name": tool_name,
            "status": "failed",
            "error_code": ErrorCode.EXECUTION_FAILED.value,
        },
        errors=(
            ToolError(
                code=ErrorCode.EXECUTION_FAILED,
                message=message,
                details={},
            ),
        ),
    )


def make_handler(*, service_name: str, tool_name: str, query_path: str, query_handler: QueryHandler) -> type[JsonHandler]:
    """Build a small JSON HTTP handler for a single observability MCP tool."""

    class ObservabilityHandler(JsonHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.is_metrics_request():
                self.write_metrics(service_name)
                return
            if self.path == "/healthz":
                self.write_json(
                    HTTPStatus.OK,
                    {
                        "service": service_name,
                        "status": "ok",
                        "tool_name": tool_name,
                        "query_path": query_path,
                    },
                )
                return

            if self.path == "/readyz":
                self.write_json(
                    HTTPStatus.OK,
                    {
                        "service": service_name,
                        "status": "ok",
                        "tool_name": tool_name,
                    },
                )
                return

            self.write_not_found()

        def do_POST(self) -> None:  # noqa: N802
            if self.path != query_path:
                self.write_not_found()
                return

            try:
                payload = self.read_json_body()
            except (ValueError, TypeError) as exc:
                self.write_json(
                    HTTPStatus.BAD_REQUEST,
                    {"service": service_name, "status": "invalid", "error": str(exc)},
                )
                return

            try:
                envelope = asyncio.run(query_handler(payload))
            except Exception as exc:
                envelope = _failure_envelope(
                    tool_name=tool_name,
                    request_id=str(payload.get("request_id") or ""),
                    correlation_id=payload.get("correlation_id"),
                    message=str(exc) or "unexpected execution failure",
                )
            self.write_json(HTTPStatus.OK, asdict(envelope))

    return ObservabilityHandler


def build_parser(*, description: str, env_host: str, env_port: str, default_port: int) -> argparse.ArgumentParser:
    """Build an argparse parser for a small observability MCP HTTP service."""
    import os

    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--host", default=os.getenv(env_host, "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv(env_port, str(default_port))))
    return parser


def run_service(
    *,
    service_name: str,
    tool_name: str,
    query_path: str,
    query_handler: QueryHandler,
    description: str,
    env_host: str,
    env_port: str,
    default_port: int,
) -> None:
    """Run a single-tool observability MCP HTTP service."""
    args = build_parser(
        description=description,
        env_host=env_host,
        env_port=env_port,
        default_port=default_port,
    ).parse_args()
    handler = make_handler(
        service_name=service_name,
        tool_name=tool_name,
        query_path=query_path,
        query_handler=query_handler,
    )
    serve(handler, host=args.host, port=args.port)
