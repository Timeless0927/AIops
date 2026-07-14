"""Three-port HTTP server adapter for the controlled verification fixture."""

from __future__ import annotations

import json
import os
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from verification_service.events import emit_event
from verification_service.fault import FaultLatch, TriggerOutcome


MAX_TRIGGER_BODY_BYTES = 1024


class VerificationServer(ThreadingHTTPServer):
    latch: FaultLatch


class _Handler(BaseHTTPRequestHandler):
    server: VerificationServer

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def send_body(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        self.send_body(status, json.dumps(payload, separators=(",", ":")).encode(), "application/json")


class LiveReadyHandler(_Handler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/livez":
            self.send_body(HTTPStatus.OK, b"live\n", "text/plain; charset=utf-8")
            return
        if self.path == "/readyz":
            snapshot = self.server.latch.snapshot()
            status = HTTPStatus.SERVICE_UNAVAILABLE if snapshot.active else HTTPStatus.OK
            self.send_body(status, b"fault_active\n" if snapshot.active else b"ready\n", "text/plain; charset=utf-8")
            return
        self.send_error(HTTPStatus.NOT_FOUND)


class MetricsHandler(_Handler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/metrics":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        snapshot = self.server.latch.snapshot()
        value = "1" if snapshot.active else "0"
        body = (
            "# HELP aiops_verification_fault_active Whether the controlled readiness fault is active.\n"
            "# TYPE aiops_verification_fault_active gauge\n"
            f'aiops_verification_fault_active{{service="verification-api",run_id="{snapshot.run_id}"}} {value}\n'
        ).encode()
        self.send_body(HTTPStatus.OK, body, "text/plain; version=0.0.4; charset=utf-8")


class TriggerHandler(_Handler):
    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/trigger":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > MAX_TRIGGER_BODY_BYTES:
                raise ValueError("invalid body size")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict) or set(payload) != {"run_id"}:
                raise ValueError("expected only run_id")
            run_id = payload["run_id"]
            if not isinstance(run_id, str):
                raise ValueError("run_id must be a string")
            outcome = self.server.latch.trigger(run_id)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_trigger"})
            return

        if outcome == TriggerOutcome.CONFLICT:
            self.send_json(HTTPStatus.CONFLICT, {"error": "fault_already_active"})
            return
        if outcome == TriggerOutcome.ACTIVATED:
            emit_event("verification_fault_activated", run_id)
        self.send_json(HTTPStatus.OK, {"run_id": run_id, "outcome": outcome.value})


def create_servers(
    latch: FaultLatch,
    host: str = "0.0.0.0",
    ports: tuple[int, int, int] = (8080, 8081, 9090),
) -> list[VerificationServer]:
    servers = [
        VerificationServer((host, ports[0]), LiveReadyHandler),
        VerificationServer((host, ports[1]), TriggerHandler),
        VerificationServer((host, ports[2]), MetricsHandler),
    ]
    for server in servers:
        server.latch = latch
    return servers


def serve() -> None:
    recovery_run_id = os.getenv("AIOPS_VERIFICATION_RUN_ID", "")
    latch = FaultLatch(recovery_run_id)
    servers = create_servers(latch)
    emit_event("verification_fault_recovered" if recovery_run_id else "verification_ready", recovery_run_id)
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers[1:]]
    for thread in threads:
        thread.start()
    try:
        servers[0].serve_forever()
    finally:
        for server in servers[1:]:
            server.shutdown()
        for server in servers:
            server.server_close()
