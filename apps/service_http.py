"""Small HTTP helpers for split-service smoke surfaces."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http import HTTPStatus
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


JSON = dict[str, Any]
_HTTP_LOCK = threading.Lock()
_HTTP_IN_FLIGHT: dict[type[BaseHTTPRequestHandler], int] = {}
_HTTP_REQUESTS: dict[type[BaseHTTPRequestHandler], dict[tuple[str, str], list[float]]] = {}
_SQLITE_ERRORS: dict[str, int] = {}
MAX_OWNER_RESPONSE_BYTES = 64 * 1024


def metrics_body(service: str, handler_type: type[BaseHTTPRequestHandler] | None = None) -> bytes:
    """Prometheus liveness and bounded-label HTTP RED metrics without a dependency."""
    safe = _label(service)
    text = (
        "# HELP aiops_service_up AIOps service liveness metric for Prometheus scrape\n"
        "# TYPE aiops_service_up gauge\n"
        f'aiops_service_up{{service="{safe}"}} 1\n'
    )
    if handler_type is not None:
        text += _http_metrics(service, handler_type)
    text += _runtime_metrics(service)
    return text.encode("utf-8")


def _http_metrics(service: str, handler_type: type[BaseHTTPRequestHandler]) -> str:
    safe = _label(service)
    with _HTTP_LOCK:
        in_flight = _HTTP_IN_FLIGHT.get(handler_type, 0)
        requests = {key: tuple(value) for key, value in _HTTP_REQUESTS.get(handler_type, {}).items()}
    lines = [
        "# HELP aiops_http_requests_in_flight Current HTTP requests",
        "# TYPE aiops_http_requests_in_flight gauge",
        f'aiops_http_requests_in_flight{{service="{safe}"}} {in_flight}',
        "# HELP aiops_http_requests_total Completed HTTP requests by bounded method and status class",
        "# TYPE aiops_http_requests_total counter",
    ]
    for (method, status), (count, _duration) in sorted(requests.items()):
        lines.append(
            f'aiops_http_requests_total{{service="{safe}",method="{method}",status="{status}"}} {int(count)}'
        )
    lines.extend((
        "# HELP aiops_http_request_duration_seconds HTTP request duration by bounded method",
        "# TYPE aiops_http_request_duration_seconds summary",
    ))
    for method in ("GET", "POST", "PATCH", "PUT", "DELETE", "OTHER"):
        values = [value for (seen_method, _status), value in requests.items() if seen_method == method]
        if not values:
            continue
        count = int(sum(value[0] for value in values))
        duration = sum(value[1] for value in values)
        labels = f'service="{safe}",method="{method}"'
        lines.append(f'aiops_http_request_duration_seconds_count{{{labels}}} {count}')
        lines.append(f'aiops_http_request_duration_seconds_sum{{{labels}}} {duration:.6f}')
    return "\n".join(lines) + "\n"


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _runtime_metrics(service: str) -> str:
    safe = _label(service)
    path = Path(os.getenv("AIOPS_DATA_DIR", "data")).expanduser().resolve()
    while not path.exists() and path != path.parent:
        path = path.parent
    usage = shutil.disk_usage(path)
    ratio = usage.free / usage.total if usage.total else 0.0
    with _HTTP_LOCK:
        sqlite_errors = _SQLITE_ERRORS.get(service, 0)
    return (
        "# HELP aiops_storage_available_ratio Available filesystem capacity for service state\n"
        "# TYPE aiops_storage_available_ratio gauge\n"
        f'aiops_storage_available_ratio{{service="{safe}"}} {ratio:.6f}\n'
        "# HELP aiops_sqlite_errors_total SQLite errors observed at the service boundary\n"
        "# TYPE aiops_sqlite_errors_total counter\n"
        f'aiops_sqlite_errors_total{{service="{safe}"}} {sqlite_errors}\n'
    )


def record_sqlite_error(service: str) -> None:
    with _HTTP_LOCK:
        _SQLITE_ERRORS[service] = _SQLITE_ERRORS.get(service, 0) + 1


def parse_csv(raw: str | None, *, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Parse comma-separated env values into a stable tuple."""
    if raw is None:
        return default
    values = tuple(part.strip() for part in raw.split(",") if part.strip())
    return values or default


def get_json(url: str, *, timeout: float = 2.0) -> JSON:
    """Fetch and decode a JSON endpoint for smoke connectivity checks."""
    with urllib.request.urlopen(url, timeout=timeout) as response:
        data = read_bounded_json(response)
    if data is None:
        raise ValueError("JSON endpoint did not return a bounded object")
    return data


def read_bounded_body(response, max_bytes: int = MAX_OWNER_RESPONSE_BYTES) -> bytes:
    payload = response.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ValueError("owner response is too large")
    return payload


def read_bounded_json(response, max_bytes: int = MAX_OWNER_RESPONSE_BYTES) -> JSON | None:
    try:
        payload = read_bounded_body(response, max_bytes)
        data = json.loads(payload.decode() or "{}")
    except (OSError, ValueError, HTTPException, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


class JsonHandler(BaseHTTPRequestHandler):
    """Base handler that emits compact JSON and emits one stdout access line per request.

    The stdlib ``BaseHTTPRequestHandler`` access log defaults to **stderr** via
    ``log_message``; alloy in ``dev-external`` scrapes pod **stdout** as the Loki
    collection surface (see ``backends/logging-guidelines`` §stdout). A sub-1 stdout
    line per request is the minimum lifecycle signal Loki needs; we do not route
    request bodies or secrets here.
    """

    server_version = "aiops-service-smoke/1.0"
    service_name = "aiops-service"

    def handle_one_request(self) -> None:
        started = time.monotonic()
        self._response_status: int | None = None
        with _HTTP_LOCK:
            _HTTP_IN_FLIGHT[type(self)] = _HTTP_IN_FLIGHT.get(type(self), 0) + 1
        try:
            super().handle_one_request()
        except sqlite3.Error:
            record_sqlite_error(self.service_name)
            if self._response_status is None:
                self.write_json(HTTPStatus.SERVICE_UNAVAILABLE, {"status": "storage_unavailable"})
            else:
                raise
        finally:
            with _HTTP_LOCK:
                _HTTP_IN_FLIGHT[type(self)] -= 1
                if self._response_status is not None:
                    key = (_bounded_method(getattr(self, "command", None)), f"{self._response_status // 100}xx")
                    values = _HTTP_REQUESTS.setdefault(type(self), {}).setdefault(key, [0.0, 0.0])
                    values[0] += 1
                    values[1] += max(0.0, time.monotonic() - started)

    def send_response(self, code: int, message: str | None = None) -> None:
        self._response_status = int(code)
        super().send_response(code, message)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        # Stay silent for incidental log_error/log_request calls; per-request stdout
        # is emitted by log_request below so alloy sees exactly one line per request.
        return

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:  # noqa: N802
        if isinstance(code, HTTPStatus):
            code = code.value
        payload = {
            "service": self.service_name,
            "event": "http_request",
            "method": _bounded_method(getattr(self, "command", None)),
            "status": int(code) if str(code).isdigit() else code,
            "request_id": self.request_id(),
            "correlation_id": self.correlation_id(),
        }
        sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    def request_id(self) -> str:
        cached = getattr(self, "_request_id", None)
        if cached:
            return str(cached)
        value = self.headers.get("X-Request-ID") or self.headers.get("X-Correlation-ID")
        self._request_id = value.strip() if value and value.strip() else f"req-{uuid.uuid4().hex}"
        return self._request_id

    def correlation_id(self) -> str:
        value = self.headers.get("X-Correlation-ID")
        return value.strip() if value and value.strip() else self.request_id()

    def read_json_body(self) -> JSON:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        payload = self.rfile.read(length).decode("utf-8")
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise ValueError("request body must be a JSON object")
        return data

    def write_json(self, status: int, payload: JSON, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def write_not_found(self) -> None:
        self.write_json(404, {"status": "not_found", "path": self.path})

    def is_metrics_request(self) -> bool:
        """True when this request targets the Prometheus scrape path ``/metrics``."""
        return urlparse(self.path).path == "/metrics"

    def write_metrics(self, service: str, extra: bytes = b"") -> None:
        """Emit Prometheus exposition response (text/plain)."""
        self.write_metrics_body(metrics_body(service, type(self)) + extra)

    def write_metrics_body(self, body: bytes) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


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


def _bounded_method(method: object) -> str:
    value = str(method or "").upper()
    return value if value in {"GET", "POST", "PATCH", "PUT", "DELETE"} else "OTHER"


if __name__ == "__main__":  # ponytail self-check: stdout access + /metrics exposition
    import io
    import threading
    import urllib.request as _ur

    class _Probe(JsonHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.is_metrics_request():
                self.write_metrics("probe")
                return
            if self.path == "/healthz":
                self.write_json(200, {"status": "ok"})
                return
            self.write_not_found()

    port = 0
    server = ThreadingHTTPServer(("127.0.0.1", port), _Probe)
    port = server.server_address[1]
    out = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = out
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        _ur.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2).read()
        mresp = _ur.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=2)
        mbody = mresp.read().decode("utf-8")
        mtype = mresp.headers.get("Content-Type", "")
    finally:
        server.shutdown()
        sys.stdout = real_stdout
    line = out.getvalue().strip().splitlines()
    assert any('"event":"http_request"' in l and '"status":200' in l for l in line), f"missing healthz access line: {line!r}"
    assert mtype.startswith("text/plain"), f"bad metrics content-type: {mtype!r}"
    assert "aiops_service_up" in mbody and 'service="probe"' in mbody, f"bad metrics body: {mbody!r}"
    print("ok: access+metrics; metrics:", "aiops_service_up" in mbody)
