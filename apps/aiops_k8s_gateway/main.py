"""Smokeable entry point for the AIOps K8s Gateway process."""

from __future__ import annotations

import argparse
import os
import urllib.error
import uuid
from dataclasses import asdict
from http import HTTPStatus

from apps.service_http import JsonHandler, connectivity_payload, post_json, serve
from aiops.contracts import ErrorCode, ToolEnvelope, ToolError

from . import APP_NAME
from .connector_router import ConnectorRoute


_ROUTES: dict[str, ConnectorRoute] = {}


class GatewayHandler(JsonHandler):
    """Minimal Gateway HTTP surface used by image and compose smoke tests."""

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self.write_json(
                HTTPStatus.OK,
                {
                    "service": APP_NAME,
                    "status": "ok",
                    "connector_url": os.getenv("AIOPS_CONNECTOR_URL", ""),
                },
            )
            return

        if self.path == "/readyz":
            self.write_json(
                HTTPStatus.OK,
                {
                    "service": APP_NAME,
                    "status": "ok",
                    "registered_connectors": len(_ROUTES),
                },
            )
            return

        if self.path == "/connectivity/connector":
            connector_url = os.getenv("AIOPS_CONNECTOR_URL", "")
            if not connector_url:
                self.write_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "service": APP_NAME,
                        "status": "unavailable",
                        "peer": "connector",
                        "error": "AIOPS_CONNECTOR_URL is not set",
                    },
                )
                return
            status, payload = connectivity_payload(
                service=APP_NAME,
                peer_name="connector",
                peer_url=connector_url,
            )
            self.write_json(status, payload)
            return

        if self.path == "/connectors":
            self.write_json(
                HTTPStatus.OK,
                {
                    "service": APP_NAME,
                    "status": "ok",
                    "connectors": [asdict(route) for route in _ROUTES.values()],
                },
            )
            return

        self.write_not_found()

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/diagnostics/read":
            self._handle_diagnostics_read()
            return

        if self.path != "/connectors/register":
            self.write_not_found()
            return

        try:
            payload = self.read_json_body()
            connector_id = str(payload["connector_id"])
            cluster_id = str(payload["cluster_id"])
        except (KeyError, ValueError, TypeError) as exc:
            self.write_json(
                HTTPStatus.BAD_REQUEST,
                {"service": APP_NAME, "status": "invalid", "error": str(exc)},
            )
            return

        route = ConnectorRoute(
            cluster_id=cluster_id,
            connector_id=connector_id,
            session_id=f"session-{uuid.uuid4().hex}",
        )
        _ROUTES[connector_id] = route
        self.write_json(
            HTTPStatus.CREATED,
            {
                "service": APP_NAME,
                "status": "registered",
                "route": asdict(route),
            },
        )

    def _handle_diagnostics_read(self) -> None:
        try:
            payload = self.read_json_body()
        except (ValueError, TypeError) as exc:
            self.write_json(
                HTTPStatus.BAD_REQUEST,
                _error_payload(
                    tool_name="diagnostics.read",
                    code=ErrorCode.INVALID_REQUEST,
                    message=str(exc),
                ),
            )
            return

        connector_url = os.getenv("AIOPS_CONNECTOR_URL", "")
        if not connector_url:
            self.write_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                _error_payload(
                    tool_name=str(payload.get("tool") or payload.get("tool_name") or "diagnostics.read"),
                    request_id=_request_id(payload),
                    code=ErrorCode.CONNECTOR_OFFLINE,
                    message="AIOPS_CONNECTOR_URL is not set",
                ),
            )
            return

        target = f"{connector_url.rstrip('/')}/diagnostics/read"
        try:
            connector_status, result = post_json(target, payload)
        except (OSError, TimeoutError, urllib.error.URLError, ValueError) as exc:
            self.write_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                _error_payload(
                    tool_name=str(payload.get("tool") or payload.get("tool_name") or "diagnostics.read"),
                    request_id=_request_id(payload),
                    code=ErrorCode.CONNECTOR_OFFLINE,
                    message=str(exc),
                    details={"target": target},
                ),
            )
            return

        result["service"] = APP_NAME
        result["connector_target"] = target
        self.write_json(_gateway_status(connector_status, result), result)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AIOps K8s Gateway service")
    parser.add_argument("--host", default=os.getenv("AIOPS_GATEWAY_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("AIOPS_GATEWAY_PORT", "8080")))
    return parser


def _request_id(payload: dict[str, object]) -> str:
    args = payload.get("args")
    if isinstance(args, dict):
        return str(args.get("request_id") or "")
    return ""


def _error_payload(
    *,
    tool_name: str,
    code: ErrorCode,
    message: str,
    request_id: str = "",
    details: dict[str, object] | None = None,
) -> dict[str, object]:
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
    return {
        "service": APP_NAME,
        "request_id": envelope.request_id,
        "tool_name": envelope.tool_name,
        "status": envelope.status,
        "summary": envelope.summary,
        "data": envelope.data,
        "evidence_refs": envelope.evidence_refs,
        "audit": envelope.audit,
        "truncated": envelope.truncated,
        "next_cursor": envelope.next_cursor,
        "errors": envelope.errors,
    }


def _gateway_status(connector_status: int, payload: dict[str, object]) -> int:
    if connector_status >= 500:
        return HTTPStatus.SERVICE_UNAVAILABLE
    if connector_status >= 400:
        return HTTPStatus.BAD_REQUEST
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        return HTTPStatus.OK
    return HTTPStatus.OK


def main() -> None:
    """Start the Gateway HTTP service."""
    args = _build_parser().parse_args()
    serve(GatewayHandler, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
