"""Smokeable entry point for the AIOps K8s Gateway process."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from dataclasses import asdict
from http import HTTPStatus
from urllib.parse import urlparse

from apps.service_http import JsonHandler, connectivity_payload, serve

from . import APP_NAME
from .alertmanager_webhook import handle_http_request
from .command_service import build_read_envelope, dispatch_read_envelope
from .connector_router import ConnectorRoute
from .diagnosis_writeback import apply_diagnosis_writeback, authorize_writeback_request, read_incident_view


_ROUTES: dict[str, ConnectorRoute] = {}


class GatewayHandler(JsonHandler):
    """Minimal Gateway HTTP surface used by image and compose smoke tests."""

    def do_GET(self) -> None:  # noqa: N802
        incident_id = _parse_incident_view_route(self.path)
        if incident_id is not None:
            denied = authorize_writeback_request(
                method="GET",
                path=self.path,
                body=b"",
                headers=dict(self.headers),
            )
            if denied is not None:
                status, payload = denied
                self.write_json(status, {"service": APP_NAME, **payload})
                return
            status, payload = asyncio.run(read_incident_view(incident_id))
            self.write_json(status, payload)
            return

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
        if self.path == "/diagnosis/writeback":
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length) if length > 0 else b""
            denied = authorize_writeback_request(
                method="POST",
                path=self.path,
                body=body,
                headers=dict(self.headers),
            )
            if denied is not None:
                status, payload = denied
                self.write_json(status, {"service": APP_NAME, **payload})
                return
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
                if not isinstance(payload, dict):
                    raise ValueError("request body must be a JSON object")
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                self.write_json(
                    HTTPStatus.BAD_REQUEST,
                    {"service": APP_NAME, "status": "invalid", "error": str(exc)},
                )
                return
            status, result = asyncio.run(apply_diagnosis_writeback(payload))
            self.write_json(status, {"service": APP_NAME, **result})
            return

        if self.path == "/k8s/read":
            connector_url = os.getenv("AIOPS_CONNECTOR_URL", "")
            if not connector_url:
                self.write_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "service": APP_NAME,
                        "status": "failed",
                        "error": {"code": "connector_offline", "message": "AIOPS_CONNECTOR_URL is not set"},
                    },
                )
                return

            try:
                payload = self.read_json_body()
                envelope = build_read_envelope(payload)
            except (TypeError, ValueError) as exc:
                self.write_json(
                    HTTPStatus.BAD_REQUEST,
                    {"service": APP_NAME, "status": "invalid", "error": str(exc)},
                )
                return

            route = next((item for item in _ROUTES.values() if item.cluster_id == envelope.cluster_id), None)
            if route is None:
                self.write_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "service": APP_NAME,
                        "status": "failed",
                        "error": {"code": "connector_offline", "message": "no connector route for cluster"},
                    },
                )
                return
            result = dispatch_read_envelope(envelope, route=route, connector_url=connector_url)
            status = HTTPStatus.OK if result.status in {"succeeded", "failed"} else HTTPStatus.BAD_REQUEST
            self.write_json(status, result.to_dict())
            return

        if self.path == "/webhooks/alertmanager":
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length) if length > 0 else b""
            status, payload = handle_http_request(body, dict(self.headers))
            self.write_json(status, payload)
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


def _parse_incident_view_route(path: str) -> str | None:
    parts = [part for part in urlparse(path).path.split("/") if part]
    if len(parts) == 2 and parts[0] == "incidents" and parts[1].strip():
        return parts[1].strip()
    return None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AIOps K8s Gateway service")
    parser.add_argument("--host", default=os.getenv("AIOPS_GATEWAY_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("AIOPS_GATEWAY_PORT", "8080")))
    return parser


def main() -> None:
    """Start the Gateway HTTP service."""
    args = _build_parser().parse_args()
    serve(GatewayHandler, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
