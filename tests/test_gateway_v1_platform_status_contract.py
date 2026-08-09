from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import jsonschema

from apps.aiops_k8s_gateway import main as gateway_main
from apps.aiops_k8s_gateway import model_provider_http, notification_admin_http
from apps.aiops_k8s_gateway.gateway_sessions import GatewaySessions


class _OwnerHandler(BaseHTTPRequestHandler):
    notification_revision = "notification-destination-revision:1"

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/model-provider/status":
            self._json({
                "model": {
                    "readiness": "ready",
                    "configuration": "present",
                    "configuration_revision": "model-provider:revision-1",
                    "verification": {
                        "operation_id": "model-verification:1",
                        "state": "verified",
                        "revision": "model-provider:revision-1",
                        "checked_at": 1_700_000_000.0,
                        "reason_code": None,
                    },
                    "availability": {
                        "state": "available", "observed_at": 1_700_000_001.0, "reason_code": None,
                        "raw_error": "must-not-leak model response",
                    },
                    "endpoint": "https://must-not-leak.example.test/v1",
                }
            })
            return
        if self.path == "/notification/status":
            self._json({
                "notification": {
                    "readiness": "not_ready",
                    "configuration": "present",
                    "configuration_revision": self.notification_revision,
                    "setup_decision": "active",
                    "verification": {
                        "operation_id": "notification-delivery:1",
                        "state": "failed",
                        "revision": self.notification_revision,
                        "checked_at": 1_700_000_002.0,
                        "reason_code": "authentication_failed",
                        "raw_error": "must-not-leak notification response",
                    },
                    "availability": {
                        "state": "unavailable",
                        "observed_at": 1_700_000_002.0,
                        "reason_code": "authentication_failed",
                    },
                    "pilot_route_selected": False,
                    "recipient": "must-not-leak@example.test",
                }
            })
            return
        if self.path.startswith("/api/v1/query"):
            self._json({"status": "success", "data": {"resultType": "vector", "result": []}})
            return
        if self.path == "/ready":
            body = b"ready"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/admin/notification-destinations":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        type(self).notification_revision = "notification-destination-revision:2"
        self._json({
            "destination": {
                "id": "destination-2",
                "configuration_revision": type(self).notification_revision,
                "readiness": "not_ready",
            }
        }, status=HTTPStatus.CREATED)

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _json(self, payload: dict[str, object], *, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _request(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, object] | None = None,
    cookie: str | None = None,
    csrf: str | None = None,
    request_id: str | None = None,
) -> tuple[int, dict[str, object], str | None]:
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie
    if csrf:
        headers["X-CSRF-Token"] = csrf
    if request_id:
        headers["X-Request-ID"] = request_id
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            return response.status, json.loads(response.read()), response.headers.get("Set-Cookie")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read()), exc.headers.get("Set-Cookie")


def _login(
    base_url: str, username: str = "admin", password: str = "admin-pass",
) -> tuple[str, str]:
    status, _, set_cookie = _request(
        f"{base_url}/auth/login",
        method="POST",
        body={"username": username, "password": password, "session_mode": "cookie"},
    )
    assert status == 200 and set_cookie
    cookie = set_cookie.split(";", 1)[0]
    status, payload, _ = _request(f"{base_url}/auth/csrf", cookie=cookie)
    assert status == 200
    return cookie, str(payload["csrf_token"])


def test_platform_status_and_durable_skip_through_gateway(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AIOPS_BOOTSTRAP_ADMIN_PASSWORD", "admin-pass")
    monkeypatch.delenv("AIOPS_IDENTITY_CONFIG", raising=False)
    monkeypatch.setattr(model_provider_http, "internal_auth_headers", lambda: {})
    monkeypatch.setattr(notification_admin_http, "internal_auth_headers", lambda: {})
    _OwnerHandler.notification_revision = "notification-destination-revision:1"

    owner = ThreadingHTTPServer(("127.0.0.1", 0), _OwnerHandler)
    owner_thread = threading.Thread(target=owner.serve_forever, daemon=True)
    owner_thread.start()
    owner_url = f"http://127.0.0.1:{owner.server_address[1]}"
    monkeypatch.setenv("AIOPS_DIAGNOSIS_URL", owner_url)
    monkeypatch.setenv("AIOPS_NOTIFICATION_ENGINE_URL", owner_url)
    monkeypatch.setenv("PROMETHEUS_URL", owner_url)
    monkeypatch.setenv("LOKI_URL", owner_url)

    gateway = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    gateway_thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    gateway_thread.start()
    base_url = f"http://127.0.0.1:{gateway.server_address[1]}"
    decision_url = f"{base_url}/api/v1/admin/platform/capabilities/notification/setup-decision"

    try:
        cookie, csrf = _login(base_url)
        create_user_status, _, _ = _request(
            f"{base_url}/api/v1/admin/users",
            method="POST",
            body={
                "username": "readonly",
                "display_name": "Read only SRE",
                "password": "readonly-pass",
                "reason": "verify public Platform Status boundary",
            },
            cookie=cookie,
            csrf=csrf,
        )
        readonly_cookie, readonly_csrf = _login(base_url, "readonly", "readonly-pass")
        anonymous_status, _, _ = _request(f"{base_url}/api/v1/platform/status")
        readonly_status, readonly, _ = _request(
            f"{base_url}/api/v1/platform/status", cookie=readonly_cookie,
        )
        stream_request = urllib.request.Request(
            f"{base_url}/api/v1/platform/status/stream",
            headers={"Cookie": readonly_cookie, "Accept": "text/event-stream"},
        )
        with urllib.request.urlopen(stream_request, timeout=3) as stream:
            stream_content_type = stream.headers.get("Content-Type")
            stream_lines = [stream.readline().decode().strip() for _ in range(2)]
        forbidden_status, forbidden, _ = _request(
            decision_url,
            method="PUT",
            body={
                "setup_decision": "skipped",
                "expected_revision": _OwnerHandler.notification_revision,
                "reason": "ordinary user cannot skip",
            },
            cookie=readonly_cookie,
            csrf=readonly_csrf,
        )
        initial_status, initial, _ = _request(f"{base_url}/api/v1/platform/status", cookie=cookie)
        invalid_status, invalid, _ = _request(
            decision_url,
            method="PUT",
            body={
                "setup_decision": "skipped",
                "expected_revision": 42,
                "reason": "reject invalid revision type",
            },
            cookie=cookie,
            csrf=csrf,
        )
        invalid_decision_status, invalid_decision, _ = _request(
            decision_url,
            method="PUT",
            body={
                "setup_decision": [],
                "expected_revision": _OwnerHandler.notification_revision,
                "reason": "reject invalid decision type",
            },
            cookie=cookie,
            csrf=csrf,
        )
        stale_status, stale, _ = _request(
            decision_url,
            method="PUT",
            body={
                "setup_decision": "skipped",
                "expected_revision": "notification-destination-revision:stale",
                "reason": "reject stale browser state",
            },
            cookie=cookie,
            csrf=csrf,
        )
        csrf_status, csrf_error, _ = _request(
            decision_url,
            method="PUT",
            body={
                "setup_decision": "skipped",
                "expected_revision": _OwnerHandler.notification_revision,
                "reason": "defer optional notification",
            },
            cookie=cookie,
        )
        skip_status, skipped, _ = _request(
            decision_url,
            method="PUT",
            body={
                "setup_decision": "skipped",
                "expected_revision": _OwnerHandler.notification_revision,
                "reason": "defer optional notification",
            },
            cookie=cookie,
            csrf=csrf,
            request_id="notification-save:2",
        )
        reopened_status, reopened, _ = _request(
            f"{base_url}/api/v1/platform/status", cookie=cookie,
        )
        save_status, _, _ = _request(
            f"{base_url}/api/v1/admin/notification-destinations",
            method="POST",
            body={
                "name": "Repaired destination",
                "provider": "feishu",
                "config": {
                    "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/new-secret",
                },
                "reason": "save repaired notification configuration",
            },
            cookie=cookie,
            csrf=csrf,
            request_id="notification-save:2",
        )
        resumed_status, resumed, _ = _request(
            f"{base_url}/api/v1/platform/status", cookie=cookie,
        )
        monkeypatch.setattr(GatewaySessions, "is_fresh", lambda _self, _token: False)
        stale_auth_status, stale_auth, _ = _request(
            decision_url,
            method="PUT",
            body={
                "setup_decision": "skipped",
                "expected_revision": _OwnerHandler.notification_revision,
                "reason": "stale authentication must not change setup",
            },
            cookie=cookie,
            csrf=csrf,
            request_id="notification-stale-auth",
        )

        assert create_user_status == 201 and readonly_status == 200
        assert stream_content_type.startswith("text/event-stream")
        assert stream_lines[0] == "event: platform_status"
        assert set(json.loads(stream_lines[1].removeprefix("data: "))["capabilities"]) == {
            "model", "notification", "connector", "observability",
        }
        assert anonymous_status == 401
        assert forbidden_status == 403 and forbidden["error"]["code"] == "forbidden"
        assert set(readonly["capabilities"]) == {"model", "notification", "connector", "observability"}
        assert invalid_status == 400 and invalid["error"]["code"] == "invalid_request"
        assert invalid_decision_status == 400
        assert invalid_decision["error"]["code"] == "invalid_request"
        assert stale_status == 409 and stale["error"]["code"] == "stale_configuration"
        assert initial_status == skip_status == reopened_status == 200
        assert set(initial["capabilities"]) == {"model", "notification", "connector", "observability"}
        assert initial["capabilities"]["observability"]["readiness"] == "ready"
        assert csrf_status == 403 and csrf_error["error"]["code"] == "csrf_required"
        assert skipped["setup_decision"]["setup_decision"] == "skipped"
        assert reopened["capabilities"]["notification"]["readiness"] == "skipped"
        assert save_status == 201 and resumed_status == 200
        assert stale_auth_status == 403
        assert stale_auth["error"]["code"] == "fresh_auth_required"
        assert resumed["capabilities"]["notification"]["setup_decision"] == "active"
        assert resumed["capabilities"]["notification"]["readiness"] == "not_ready"
        serialized = json.dumps(reopened)
        assert "must-not-leak" not in serialized
        assert "all_ready" not in serialized and "setup_complete" not in serialized
        spec = json.loads(Path("api/openapi/gateway-v1.json").read_text(encoding="utf-8"))
        resolver = jsonschema.RefResolver.from_schema(spec)
        for schema_name, payload in (
            ("PlatformStatusResponse", initial),
            ("PlatformSetupDecisionResponse", skipped),
        ):
            jsonschema.Draft202012Validator(
                spec["components"]["schemas"][schema_name], resolver=resolver,
            ).validate(payload)
        assert spec["paths"]["/api/v1/platform/status"]["get"]["operationId"] == "getPlatformStatus"
        assert spec["paths"]["/api/v1/platform/status/stream"]["get"]["operationId"] == "streamPlatformStatus"
        assert (
            spec["paths"]["/api/v1/admin/platform/capabilities/notification/setup-decision"]
            ["put"]["operationId"] == "setNotificationSetupDecision"
        )
    finally:
        gateway.shutdown()
        gateway.server_close()
        gateway_thread.join(timeout=2)
        owner.shutdown()
        owner.server_close()
        owner_thread.join(timeout=2)
