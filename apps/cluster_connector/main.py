"""Smokeable entry point for the Cluster Connector process."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import urllib.error
import urllib.request
from dataclasses import asdict
from http import HTTPStatus
from typing import Any

from apps.service_http import JsonHandler, parse_csv, serve, to_jsonable
from aiops.contracts import ErrorCode, ToolEnvelope, ToolError

from . import APP_NAME
from .stream_client import ConnectorRegistration


SUPPORTED_READ_TOOLS = {"query_metrics", "query_logs", "run_k8s_read"}


def _registration() -> ConnectorRegistration:
    return ConnectorRegistration(
        connector_id=os.getenv("AIOPS_CONNECTOR_ID", "connector-local"),
        cluster_id=os.getenv("AIOPS_CLUSTER_ID", "cluster-local"),
        namespace_scope=parse_csv(os.getenv("AIOPS_NAMESPACE_SCOPE"), default=("default",)),
        capabilities=parse_csv(
            os.getenv("AIOPS_CONNECTOR_CAPABILITIES"),
            default=("health", "validate"),
        ),
    )


def _register_with_gateway(gateway_url: str, registration: ConnectorRegistration) -> bool:
    if not gateway_url:
        return False
    body = json.dumps(asdict(registration)).encode("utf-8")
    request = urllib.request.Request(
        f"{gateway_url.rstrip('/')}/connectors/register",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return 200 <= response.status < 300
    except (OSError, TimeoutError, urllib.error.URLError):
        return False


class ConnectorHandler(JsonHandler):
    """Minimal Connector HTTP surface used by image and compose smoke tests."""

    registration: ConnectorRegistration
    registered_with_gateway: bool = False

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self.write_json(
                HTTPStatus.OK,
                {
                    "service": APP_NAME,
                    "status": "ok",
                    "registration": asdict(self.registration),
                    "registered_with_gateway": self.registered_with_gateway,
                },
            )
            return

        if self.path == "/readyz":
            self.write_json(
                HTTPStatus.OK,
                {
                    "service": APP_NAME,
                    "status": "ok",
                    "registered_with_gateway": self.registered_with_gateway,
                },
            )
            return

        self.write_not_found()

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/diagnostics/read":
            self.write_not_found()
            return

        try:
            payload = self.read_json_body()
            tool = str(payload.get("tool") or payload.get("tool_name") or "")
            args = payload.get("args") or {}
            if not isinstance(args, dict):
                raise ValueError("args must be a JSON object")
            result = _run_read_tool(tool, args, self.registration)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            result = _error_envelope(
                request_id="",
                tool_name="diagnostics.read",
                code=ErrorCode.INVALID_REQUEST,
                message=str(exc),
            )

        http_status = HTTPStatus.OK
        errors = result.get("errors")
        if (
            isinstance(errors, list)
            and errors
            and isinstance(errors[0], dict)
            and errors[0].get("code") == ErrorCode.INVALID_REQUEST.value
        ):
            http_status = HTTPStatus.BAD_REQUEST
        self.write_json(http_status, result)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AIOps Cluster Connector service")
    parser.add_argument("--host", default=os.getenv("AIOPS_CONNECTOR_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("AIOPS_CONNECTOR_PORT", "8081")))
    return parser


def _run_read_tool(tool: str, args: dict[str, Any], registration: ConnectorRegistration) -> dict[str, Any]:
    if tool not in SUPPORTED_READ_TOOLS:
        return _error_envelope(
            request_id=str(args.get("request_id") or ""),
            tool_name=tool or "diagnostics.read",
            code=ErrorCode.INVALID_REQUEST,
            message=f"unsupported read-only diagnostic tool: {tool or '<missing>'}",
            details={"supported_tools": sorted(SUPPORTED_READ_TOOLS)},
        )

    args = dict(args)
    args.setdefault("cluster_id", registration.cluster_id)
    if registration.namespace_scope:
        args.setdefault("namespace_scope", list(registration.namespace_scope))
    if tool == "run_k8s_read":
        args.setdefault("connector_id", registration.connector_id)

    try:
        if tool == "query_metrics":
            from apps.mcp_prometheus.facade import query_metrics

            envelope = asyncio.run(query_metrics(args))
            payload = _tool_envelope_payload(envelope)
        elif tool == "query_logs":
            from apps.mcp_loki.facade import query_logs

            envelope = asyncio.run(query_logs(args))
            payload = _tool_envelope_payload(envelope)
        else:
            from toolsets.k8s_read import run_k8s_read

            payload = asyncio.run(run_k8s_read(**args))
            payload.setdefault("tool_name", "run_k8s_read")
            payload.setdefault("request_id", args.get("request_id", ""))
            payload.setdefault("evidence_refs", [])
            payload.setdefault("errors", _result_envelope_errors(payload))
    except Exception as exc:
        payload = _error_envelope(
            request_id=str(args.get("request_id") or ""),
            tool_name=tool,
            code=ErrorCode.BACKEND_UNAVAILABLE,
            message=str(exc),
        )

    payload["service"] = APP_NAME
    payload["connector"] = {
        "connector_id": registration.connector_id,
        "cluster_id": registration.cluster_id,
        "namespace_scope": list(registration.namespace_scope),
    }
    return payload


def _tool_envelope_payload(envelope: ToolEnvelope) -> dict[str, Any]:
    return to_jsonable({
        "request_id": envelope.request_id,
        "correlation_id": envelope.correlation_id,
        "tool_name": envelope.tool_name,
        "status": envelope.status,
        "summary": envelope.summary,
        "data": envelope.data,
        "evidence_refs": envelope.evidence_refs,
        "audit": envelope.audit,
        "truncated": envelope.truncated,
        "next_cursor": envelope.next_cursor,
        "errors": envelope.errors,
    })


def _error_envelope(
    *,
    request_id: str,
    tool_name: str,
    code: ErrorCode,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    envelope = ToolEnvelope(
        request_id=request_id,
        tool_name=tool_name,
        status="failed",
        summary=message,
        data={},
        evidence_refs=(),
        audit={"status": "failed", "error_code": code.value},
        errors=(ToolError(code=code, message=message, details=details or {}),),
    )
    return _tool_envelope_payload(envelope)


def _result_envelope_errors(payload: dict[str, Any]) -> list[dict[str, str]]:
    error = payload.get("error")
    if not isinstance(error, dict):
        return []
    return [
        {
            "code": str(error.get("code") or ErrorCode.EXECUTION_FAILED.value),
            "message": str(error.get("message") or "run_k8s_read failed"),
            "details": {},
        }
    ]


def main() -> None:
    """Start the Connector HTTP service."""
    args = _build_parser().parse_args()
    ConnectorHandler.registration = _registration()
    ConnectorHandler.registered_with_gateway = _register_with_gateway(
        os.getenv("AIOPS_GATEWAY_URL", ""),
        ConnectorHandler.registration,
    )
    serve(ConnectorHandler, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
