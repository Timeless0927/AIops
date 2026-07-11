"""Smokeable entry point for the Cluster Connector process."""

from __future__ import annotations

import argparse
import json
import os
import threading
from dataclasses import asdict
from http import HTTPStatus
from pathlib import Path

from apps.service_http import JsonHandler, parse_csv, serve

from aiops.k8s import CommandEnvelope

from . import APP_NAME
from .kubectl_executor import execute_command_envelope, rejected_result
from .gateway_client import sync_gateway_registration
from .command_worker import ConnectorCommandJournal, run_command_cycle
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


def _registration_loop(
    gateway_url: str,
    registration: ConnectorRegistration,
    credential: str,
    interval_seconds: float,
    stop: threading.Event,
    allow_insecure: bool = False,
) -> None:
    while not stop.wait(interval_seconds):
        ConnectorHandler.registered_with_gateway = sync_gateway_registration(
            gateway_url,
            registration,
            credential,
            allow_insecure=allow_insecure,
        )


def _command_loop(
    gateway_url: str,
    registration: ConnectorRegistration,
    credential: str,
    journal: ConnectorCommandJournal,
    allow_insecure: bool,
    stop: threading.Event,
) -> None:
    while not stop.is_set():
        run_command_cycle(
            gateway_url,
            connector_id=registration.connector_id,
            cluster_id=registration.cluster_id,
            credential=credential,
            allowed_namespaces=set(registration.namespace_scope),
            journal=journal,
            allow_insecure=allow_insecure,
        )
        if stop.wait(1.0):
            return


class ConnectorHandler(JsonHandler):
    """Minimal Connector HTTP surface used by image and compose smoke tests."""

    registration: ConnectorRegistration
    gateway_url: str = ""
    gateway_credential: str = ""
    allow_insecure_gateway: bool = False
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
            type(self).registered_with_gateway = sync_gateway_registration(
                type(self).gateway_url,
                type(self).registration,
                type(self).gateway_credential,
                allow_insecure=type(self).allow_insecure_gateway,
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
    ConnectorHandler.gateway_url = os.getenv("AIOPS_CONNECTOR_GATEWAY_URL", os.getenv("AIOPS_GATEWAY_URL", ""))
    ConnectorHandler.gateway_credential = os.getenv("AIOPS_CONNECTOR_CREDENTIAL", "")
    allow_insecure = os.getenv("AIOPS_CONNECTOR_ALLOW_INSECURE_GATEWAY", "").strip().lower() in {"1", "true", "yes", "on"}
    ConnectorHandler.allow_insecure_gateway = allow_insecure
    ConnectorHandler.registered_with_gateway = sync_gateway_registration(
        ConnectorHandler.gateway_url,
        ConnectorHandler.registration,
        ConnectorHandler.gateway_credential,
        allow_insecure=allow_insecure,
    )
    threading.Thread(
        target=_registration_loop,
        args=(
            ConnectorHandler.gateway_url,
            ConnectorHandler.registration,
            ConnectorHandler.gateway_credential,
            max(5.0, float(os.getenv("AIOPS_CONNECTOR_HEARTBEAT_SECONDS", "30"))),
            threading.Event(),
            allow_insecure,
        ),
        daemon=True,
        name="connector-heartbeat",
    ).start()
    journal = ConnectorCommandJournal(Path(os.getenv("AIOPS_DATA_DIR", "data")) / "connector.db")
    threading.Thread(
        target=_command_loop,
        args=(
            ConnectorHandler.gateway_url,
            ConnectorHandler.registration,
            ConnectorHandler.gateway_credential,
            journal,
            allow_insecure,
            threading.Event(),
        ),
        daemon=True,
        name="connector-command-poll",
    ).start()
    serve(ConnectorHandler, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
