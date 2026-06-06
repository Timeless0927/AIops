"""Small HTTP helpers for split-service smoke surfaces."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


JSON = dict[str, Any]


def parse_csv(raw: str | None, *, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Parse comma-separated env values into a stable tuple."""
    if raw is None:
        return default
    values = tuple(part.strip() for part in raw.split(",") if part.strip())
    return values or default


def get_json(url: str, *, timeout: float = 2.0) -> JSON:
    """Fetch and decode a JSON endpoint for smoke connectivity checks."""
    with urllib.request.urlopen(url, timeout=timeout) as response:
        payload = response.read().decode("utf-8")
        data = json.loads(payload or "{}")
        if not isinstance(data, dict):
            raise ValueError("JSON endpoint did not return an object")
        return data


class JsonHandler(BaseHTTPRequestHandler):
    """Base handler that emits compact JSON and suppresses default access logs."""

    server_version = "aiops-service-smoke/1.0"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return

    def read_json_body(self) -> JSON:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        payload = self.rfile.read(length).decode("utf-8")
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise ValueError("request body must be a JSON object")
        return data

    def write_json(self, status: int, payload: JSON) -> None:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def write_not_found(self) -> None:
        self.write_json(404, {"status": "not_found", "path": self.path})


def connectivity_payload(
    *,
    service: str,
    peer_name: str,
    peer_url: str,
    path: str = "/healthz",
) -> tuple[int, JSON]:
    """Return a status and payload for a peer connectivity probe."""
    target = f"{peer_url.rstrip('/')}{path}"
    try:
        response = get_json(target)
    except (OSError, TimeoutError, urllib.error.URLError, ValueError) as exc:
        return 503, {
            "service": service,
            "status": "unavailable",
            "peer": peer_name,
            "target": target,
            "error": str(exc),
        }
    return 200, {
        "service": service,
        "status": "ok",
        "peer": peer_name,
        "target": target,
        "peer_status": response.get("status"),
    }


def serve(handler: type[BaseHTTPRequestHandler], *, host: str, port: int) -> None:
    """Serve a handler until the process receives a signal."""
    server = ThreadingHTTPServer((host, port), handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
