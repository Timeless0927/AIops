from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from aiops.acceptance.http import GatewaySession


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = json.dumps({"status": "ok"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", "aiops_session=must-not-be-returned; HttpOnly")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


def test_gateway_session_ignores_environment_proxy_and_drops_cookie_headers(monkeypatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        response = GatewaySession(f"http://127.0.0.1:{server.server_port}").request("GET", "/")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert response.status == 200
    assert response.body == {"status": "ok"}
    assert response.headers == {"content-type": "application/json"}
