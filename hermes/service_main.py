"""Smokeable Hermes service boundary for split-image packaging."""

from __future__ import annotations

import argparse
import os
import urllib.error
from http import HTTPStatus

from apps.service_http import JsonHandler, connectivity_payload, post_json, serve
from aiops.contracts import ErrorCode, ToolEnvelope, ToolError


class HermesServiceHandler(JsonHandler):
    """Minimal Hermes HTTP surface used by image and compose smoke tests."""

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self.write_json(
                HTTPStatus.OK,
                {
                    "service": "hermes",
                    "status": "ok",
                    "gateway_url": os.getenv("AIOPS_GATEWAY_URL", ""),
                },
            )
            return

        if self.path in {"/readyz", "/connectivity/gateway"}:
            gateway_url = os.getenv("AIOPS_GATEWAY_URL", "")
            if not gateway_url:
                self.write_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "service": "hermes",
                        "status": "unavailable",
                        "peer": "gateway",
                        "error": "AIOPS_GATEWAY_URL is not set",
                    },
                )
                return
            status, payload = connectivity_payload(
                service="hermes",
                peer_name="gateway",
                peer_url=gateway_url,
            )
            self.write_json(status, payload)
            return

        self.write_not_found()

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/diagnostics/gateway":
            self.write_not_found()
            return

        try:
            payload = self.read_json_body()
        except (ValueError, TypeError) as exc:
            self.write_json(
                HTTPStatus.BAD_REQUEST,
                _gateway_error_payload(
                    tool_name="diagnostics.gateway",
                    code=ErrorCode.INVALID_REQUEST,
                    message=str(exc),
                ),
            )
            return

        gateway_url = os.getenv("AIOPS_GATEWAY_URL", "")
        if not gateway_url:
            self.write_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                _gateway_error_payload(
                    tool_name=str(payload.get("tool") or payload.get("tool_name") or "diagnostics.gateway"),
                    request_id=_request_id(payload),
                    code=ErrorCode.BACKEND_UNAVAILABLE,
                    message="AIOPS_GATEWAY_URL is not set",
                ),
            )
            return

        target = f"{gateway_url.rstrip('/')}/diagnostics/read"
        try:
            gateway_status, diagnostic = post_json(target, payload)
        except (OSError, TimeoutError, urllib.error.URLError, ValueError) as exc:
            self.write_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                _gateway_error_payload(
                    tool_name=str(payload.get("tool") or payload.get("tool_name") or "diagnostics.gateway"),
                    request_id=_request_id(payload),
                    code=ErrorCode.BACKEND_UNAVAILABLE,
                    message=str(exc),
                    details={"target": target},
                ),
            )
            return

        status = str(diagnostic.get("status") or "failed")
        self.write_json(
            _hermes_status(gateway_status),
            {
                "service": "hermes",
                "status": "ok" if gateway_status < 500 else "failed",
                "gateway_status": status,
                "gateway_target": target,
                "diagnostic": diagnostic,
            },
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AIOps Hermes service smoke boundary")
    parser.add_argument("--host", default=os.getenv("AIOPS_HERMES_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("AIOPS_HERMES_PORT", "8082")))
    return parser


def _request_id(payload: dict[str, object]) -> str:
    args = payload.get("args")
    if isinstance(args, dict):
        return str(args.get("request_id") or "")
    return ""


def _gateway_error_payload(
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
        "service": "hermes",
        "status": "failed",
        "gateway_status": "failed",
        "diagnostic": {
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
        },
    }


def _hermes_status(gateway_status: int) -> int:
    if gateway_status >= 500:
        return HTTPStatus.SERVICE_UNAVAILABLE
    if gateway_status >= 400:
        return HTTPStatus.BAD_REQUEST
    return HTTPStatus.OK


def main() -> None:
    args = _build_parser().parse_args()
    serve(HermesServiceHandler, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
