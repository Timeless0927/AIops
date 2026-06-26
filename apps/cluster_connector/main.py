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

from aiops.k8s import CommandEnvelope

from . import APP_NAME
from .kubectl_executor import execute_command_envelope, rejected_result
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
        if self.is_metrics_request():
            self.write_metrics(APP_NAME)
            return
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

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/commands/execute":
            self.write_not_found()
            return

        payload = {}
        try:
            payload = self.read_json_body()
            envelope = CommandEnvelope.from_dict(payload)
        except (TypeError, ValueError) as exc:
            fallback = CommandEnvelope(
                envelope_version="v1",
                task_id=str(payload.get("task_id") or "invalid-task") if isinstance(payload, dict) else "invalid-task",
                command_id=str(payload.get("command_id") or "invalid-command") if isinstance(payload, dict) else "invalid-command",
                cluster_id=str(payload.get("cluster_id") or type(self).registration.cluster_id) if isinstance(payload, dict) else type(self).registration.cluster_id,
                namespace=str(payload.get("namespace") or "unknown") if isinstance(payload, dict) else "unknown",
                action_type=str(payload.get("action_type") or "read") if isinstance(payload, dict) else "read",
                argv=("kubectl", "get", "pods"),
                timeout_seconds=1,
                output_limit_bytes=1,
                grant_id="invalid",
            )
            result = rejected_result(
                fallback,
                connector_id=type(self).registration.connector_id,
                error_code="command_rejected",
                error_message=str(exc),
            )
            self.write_json(HTTPStatus.BAD_REQUEST, result.to_dict())
            return

        result = execute_command_envelope(
            envelope,
            connector_id=type(self).registration.connector_id,
            connector_cluster_id=type(self).registration.cluster_id,
            allowed_namespaces=set(type(self).registration.namespace_scope),
        )
        status = HTTPStatus.OK if result.status in {"succeeded", "failed"} else HTTPStatus.BAD_REQUEST
        self.write_json(status, result.to_dict())


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
