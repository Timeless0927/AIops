"""Gateway-to-Diagnosis planning transport."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest

from apps.aiops_k8s_gateway import change_request_http
from apps.aiops_k8s_gateway.change_requests import ChangeRequestError


class _Handler(BaseHTTPRequestHandler):
    status = 200
    body: dict[str, object] = {"service": "diagnosis", "status": "needs_input", "question": "恢复到哪个版本？"}
    delay = 0.0
    last_timeout: float | None = None

    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.delay:
            threading.Event().wait(self.delay)
        payload = json.dumps(self.body, ensure_ascii=False).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


def _serve() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_planning_timeout_covers_model_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, float] = {}

    def fake_urlopen(request, timeout=None):  # noqa: ANN001
        captured["timeout"] = float(timeout)
        raise TimeoutError("planner wait")

    monkeypatch.setattr(change_request_http.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("AIOPS_DIAGNOSIS_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(change_request_http, "internal_auth_headers", lambda: {})
    with pytest.raises(ChangeRequestError, match="Diagnosis planning request failed") as caught:
        change_request_http.send_plan_request(
            {
                "change_request_id": "change-1",
                "incident_id": "incident-1",
                "desired_outcome": "恢复服务",
                "context": "",
                "facts": {},
                "inputs": [],
            },
            "req-1",
        )
    assert caught.value.code == "planner_unavailable"
    assert captured["timeout"] == 130


def test_planning_invalid_plan_is_not_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _Handler.status = 400
    _Handler.body = {
        "service": "diagnosis",
        "status": "rejected",
        "error": {
            "code": "invalid_plan",
            "message": "Change planning model returned invalid data: controlled restart must use the canonical annotation patch and post-checks",
        },
    }
    server = _serve()
    monkeypatch.setenv("AIOPS_DIAGNOSIS_URL", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setattr(change_request_http, "internal_auth_headers", lambda: {})
    try:
        with pytest.raises(ChangeRequestError, match="canonical annotation") as caught:
            change_request_http.send_plan_request(
                {
                    "change_request_id": "change-1",
                    "incident_id": "incident-1",
                    "desired_outcome": "受控重启",
                    "context": "",
                    "facts": {},
                    "inputs": [],
                },
                "req-1",
            )
        assert caught.value.code == "invalid_plan"
    finally:
        server.shutdown()
        server.server_close()
