"""Small HTTP helpers for split-service smoke surfaces."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import asdict, is_dataclass
from enum import Enum
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


def post_json(url: str, payload: JSON, *, timeout: float = 3.0) -> tuple[int, JSON]:
    """POST a JSON object and decode the JSON object response."""
    body = json.dumps(to_jsonable(payload), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            data = json.loads(raw or "{}")
            if not isinstance(data, dict):
                raise ValueError("JSON endpoint did not return an object")
            return response.status, data
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        data = json.loads(raw or "{}")
        if not isinstance(data, dict):
            raise ValueError("JSON endpoint did not return an object")
        return exc.code, data


def to_jsonable(value: Any) -> Any:
    """Convert dataclasses and enums into compact JSON-safe structures."""
    if is_dataclass(value):
        return to_jsonable(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    return value


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
        body = json.dumps(
            to_jsonable(payload),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
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
