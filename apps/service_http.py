"""Small HTTP helpers for split-service smoke surfaces."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from http import HTTPStatus
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
    """Base handler that emits compact JSON and emits one stdout access line per request.

    The stdlib ``BaseHTTPRequestHandler`` access log defaults to **stderr** via
    ``log_message``; alloy in ``dev-external`` scrapes pod **stdout** as the Loki
    collection surface (see ``backends/logging-guidelines`` §stdout). A sub-1 stdout
    line per request is the minimum lifecycle signal Loki needs; we do not route
    request bodies or secrets here.
    """

    server_version = "aiops-service-smoke/1.0"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        # Stay silent for incidental log_error/log_request calls; per-request stdout
        # is emitted by log_request below so alloy sees exactly one line per request.
        return

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:  # noqa: N802
        if isinstance(code, HTTPStatus):
            code = code.value
        # ponytail: stdout (not stderr) is the Loki/alloy scrape surface; one line/req.
        sys.stdout.write(
            f'{self.address_string()} "{self.requestline}" {code} {size}\n'
        )
        sys.stdout.flush()

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


if __name__ == "__main__":  # ponytail self-check: one stdout access line per request
    import io
    import threading
    import urllib.request as _ur

    class _Probe(JsonHandler):
        def do_GET(self) -> None:  # noqa: N802
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
    finally:
        server.shutdown()
        sys.stdout = real_stdout
    line = out.getvalue().strip()
    assert line, "expected one stdout access line per request, got nothing"
    assert "/healthz" in line and " 200 " in line, f"unexpected access line: {line!r}"
    print("ok:", line)
