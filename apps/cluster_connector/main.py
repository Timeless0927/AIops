"""Smokeable entry point for the Cluster Connector process."""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from dataclasses import asdict
from http import HTTPStatus

from apps.service_http import JsonHandler, parse_csv, serve

from . import APP_NAME
from .stream_client import ConnectorRegistration


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


def _gateway_has_registration(gateway_url: str, connector_id: str) -> bool:
    if not gateway_url:
        return False
    request = urllib.request.Request(
        f"{gateway_url.rstrip('/')}/connectors",
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except (OSError, TimeoutError, urllib.error.URLError, ValueError, json.JSONDecodeError):
        return False

    connectors = payload.get("connectors")
    if not isinstance(connectors, list):
        return False
    return any(
        isinstance(connector, dict) and connector.get("connector_id") == connector_id
        for connector in connectors
    )


def _sync_gateway_registration(gateway_url: str, registration: ConnectorRegistration) -> bool:
    if not gateway_url:
        return False
    if _gateway_has_registration(gateway_url, registration.connector_id):
        return True
    return _register_with_gateway(gateway_url, registration)


class ConnectorHandler(JsonHandler):
    """Minimal Connector HTTP surface used by image and compose smoke tests."""

    registration: ConnectorRegistration
    gateway_url: str = ""
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
            type(self).registered_with_gateway = _sync_gateway_registration(
                type(self).gateway_url,
                type(self).registration,
            )
            is_registered = type(self).registered_with_gateway
            has_gateway = bool(type(self).gateway_url)
            status = HTTPStatus.OK if is_registered or not has_gateway else HTTPStatus.SERVICE_UNAVAILABLE
            self.write_json(
                status,
                {
                    "service": APP_NAME,
                    "status": "ok" if is_registered or not has_gateway else "unavailable",
                    "registered_with_gateway": is_registered,
                },
            )
            return

        self.write_not_found()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AIOps Cluster Connector service")
    parser.add_argument("--host", default=os.getenv("AIOPS_CONNECTOR_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("AIOPS_CONNECTOR_PORT", "8081")))
    return parser


def main() -> None:
    """Start the Connector HTTP service."""
    args = _build_parser().parse_args()
    ConnectorHandler.registration = _registration()
    ConnectorHandler.gateway_url = os.getenv("AIOPS_GATEWAY_URL", "")
    ConnectorHandler.registered_with_gateway = _sync_gateway_registration(
        ConnectorHandler.gateway_url,
        ConnectorHandler.registration,
    )
    serve(ConnectorHandler, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
