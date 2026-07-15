"""Smokeable entry point for the Cluster Connector process."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import threading
import time
from dataclasses import asdict
from http import HTTPStatus
from pathlib import Path

from apps.service_http import JsonHandler, parse_csv, record_sqlite_error, serve

from . import APP_NAME
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
            default=("health", "validate", "execute"),
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
    next_cleanup_at = 0.0
    while not stop.is_set():
        try:
            monotonic_now = time.monotonic()
            if monotonic_now >= next_cleanup_at:
                journal.cleanup_expired()
                next_cleanup_at = monotonic_now + 60 * 60
            run_command_cycle(
                gateway_url,
                connector_id=registration.connector_id,
                cluster_id=registration.cluster_id,
                credential=credential,
                allowed_namespaces=set(registration.namespace_scope),
                journal=journal,
                allow_insecure=allow_insecure,
                clock=time.time,
            )
        except sqlite3.Error:
            record_sqlite_error(APP_NAME)
        if stop.wait(1.0):
            return


class ConnectorHandler(JsonHandler):
    """Minimal Connector HTTP surface used by image and compose smoke tests."""

    service_name = APP_NAME
    registration: ConnectorRegistration
    gateway_url: str = ""
    gateway_credential: str = ""
    allow_insecure_gateway: bool = False
    registered_with_gateway: bool = False
    journal: ConnectorCommandJournal | None = None

    def do_GET(self) -> None:  # noqa: N802
        if self.is_metrics_request():
            extra = type(self).journal.metrics().encode() if type(self).journal is not None else b""
            self.write_metrics(APP_NAME, extra)
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
            is_registered = type(self).registered_with_gateway
            self.write_json(
                HTTPStatus.OK,
                {
                    "service": APP_NAME,
                    "status": "ok",
                    "registered_with_gateway": is_registered,
                },
            )
            return

        self.write_not_found()

    def do_POST(self) -> None:  # noqa: N802
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
    ConnectorHandler.journal = journal
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
