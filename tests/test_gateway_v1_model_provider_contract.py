from __future__ import annotations

import io
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import jsonschema
import pytest

from apps.aiops_k8s_gateway import main as gateway_main
from apps.aiops_k8s_gateway import model_provider_http
from diagnosis_service import service_main as diagnosis_main
from diagnosis_service.model_provider import VerificationResult
from tests.model_provider_support import build_test_model_provider


def _request(
    url: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    cookie: str | None = None,
    csrf: str | None = None,
    request_id: str | None = None,
) -> tuple[int, dict, str | None]:
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
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read()), response.headers.get("Set-Cookie")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read()), exc.headers.get("Set-Cookie")


def _login(base_url: str) -> tuple[str, str]:
    status, _, set_cookie = _request(
        f"{base_url}/auth/login",
        method="POST",
        body={"username": "admin", "password": "admin-pass", "session_mode": "cookie"},
    )
    assert status == 200 and set_cookie
    cookie = set_cookie.split(";", 1)[0]
    status, payload, _ = _request(f"{base_url}/auth/csrf", cookie=cookie)
    assert status == 200
    return cookie, payload["csrf_token"]


def test_model_status_rejects_oversized_owner_response(monkeypatch) -> None:
    class Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    payload = json.dumps({"model": {}, "padding": "x" * (64 * 1024)}).encode()
    monkeypatch.setattr(model_provider_http.request, "urlopen", lambda *_args, **_kwargs: Response(payload))

    with pytest.raises(OSError, match="unavailable"):
        model_provider_http.read_status("model-status:oversized")


def test_model_provider_management_and_safe_status_through_gateway(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AIOPS_BOOTSTRAP_ADMIN_PASSWORD", "admin-pass")
    monkeypatch.delenv("AIOPS_IDENTITY_CONFIG", raising=False)
    key = tmp_path / "model-key"
    key.write_bytes(b"k" * 32)
    owner = build_test_model_provider(tmp_path / "diagnosis.db", key, clock=lambda: 1_700_000_000.0)
    monkeypatch.setattr(diagnosis_main, "_MODEL_PROVIDER", owner)
    monkeypatch.setattr(diagnosis_main, "enforce_internal_auth", lambda *_args, **_kwargs: "gateway-identity")
    monkeypatch.setattr(model_provider_http, "internal_auth_headers", lambda: {})
    gateway_main._SESSIONS.clear()

    diagnosis_server = ThreadingHTTPServer(("127.0.0.1", 0), diagnosis_main.DiagnosisServiceHandler)
    diagnosis_thread = threading.Thread(target=diagnosis_server.serve_forever, daemon=True)
    diagnosis_thread.start()
    monkeypatch.setenv("AIOPS_DIAGNOSIS_URL", f"http://127.0.0.1:{diagnosis_server.server_address[1]}")
    gateway_server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    gateway_thread = threading.Thread(target=gateway_server.serve_forever, daemon=True)
    gateway_thread.start()
    base_url = f"http://127.0.0.1:{gateway_server.server_address[1]}"

    try:
        cookie, csrf = _login(base_url)
        initial_status, initial, _ = _request(f"{base_url}/api/v1/model-provider/status", cookie=cookie)
        save_status, saved, _ = _request(
            f"{base_url}/api/v1/admin/model-provider",
            method="PUT",
            body={
                "endpoint": "https://models.example.test/v1",
                "endpoint_scope": "external",
                "model": "ops-model",
                "timeout_seconds": 30,
                "api_key": "secret-provider-key",
                "image_input_supported": True,
                "expected_revision": None,
                "reason": "configure diagnosis model",
            },
            cookie=cookie,
            csrf=csrf,
            request_id="configure-model-1",
        )
        revision = saved["model_provider"]["configuration_revision"]
        replay_status, replayed, _ = _request(
            f"{base_url}/api/v1/admin/model-provider",
            method="PUT",
            body={
                "endpoint": "https://models.example.test/v1",
                "endpoint_scope": "external",
                "model": "ops-model",
                "timeout_seconds": 30,
                "api_key": "secret-provider-key",
                "image_input_supported": True,
                "expected_revision": None,
                "reason": "configure diagnosis model",
            },
            cookie=cookie,
            csrf=csrf,
            request_id="configure-model-1",
        )
        test_status, started, _ = _request(
            f"{base_url}/api/v1/admin/model-provider/test",
            method="POST",
            body={"expected_revision": revision, "reason": "verify model tool use"},
            cookie=cookie,
            csrf=csrf,
            request_id="verify-model-1",
        )

        assert initial_status == save_status == replay_status == 200
        assert replayed["model_provider"]["configuration_revision"] == revision
        assert initial["model"]["configuration"] == "absent"
        assert saved["model_provider"]["configuration"]["credential_configured"] is True
        assert "secret-provider-key" not in json.dumps(saved)
        assert test_status == 202
        assert started["verification"] == {
            "operation_id": "model-verification:verify-model-1",
            "revision": revision,
            "state": "verifying",
        }

        owner.run_verification_once(
            lambda _provider, _nonce: VerificationResult.succeeded(
                latency_ms=42,
                provider_summary="tool_use_and_structured_json_verified",
                image_input_supported=True,
            )
        )
        detail_status, detail, _ = _request(
            f"{base_url}/api/v1/admin/model-provider",
            cookie=cookie,
        )
        public_status, public, _ = _request(f"{base_url}/api/v1/model-provider/status", cookie=cookie)
        conflict_status, conflict, _ = _request(
            f"{base_url}/api/v1/admin/model-provider/test",
            method="POST",
            body={"expected_revision": "model-provider:stale", "reason": "stale browser"},
            cookie=cookie,
            csrf=csrf,
        )
        extra_status, extra, _ = _request(
            f"{base_url}/api/v1/admin/model-provider/test",
            method="POST",
            body={
                "expected_revision": revision,
                "reason": "reject ambiguous request",
                "unexpected": True,
            },
            cookie=cookie,
            csrf=csrf,
        )
        internal_extra_status, internal_extra, _ = _request(
            f"http://127.0.0.1:{diagnosis_server.server_address[1]}/admin/model-provider/test",
            method="POST",
            body={
                "actor_id": "user:admin",
                "operation_id": "verify:extra-field",
                "expected_revision": revision,
                "unexpected": True,
            },
        )

        assert detail_status == public_status == 200
        assert detail["model_provider"]["readiness"] == "ready"
        assert public["model"]["readiness"] == "ready"
        assert detail["model_provider"]["configuration"]["image_input_supported"] is True
        assert public["model"]["image_input_supported"] is True
        assert "endpoint" not in json.dumps(public)
        assert conflict_status == 409
        assert conflict["error"]["code"] == "revision_conflict"
        assert extra_status == internal_extra_status == 400
        assert extra["error"]["code"] == "invalid_request"
        assert internal_extra["error"]["code"] == "invalid_request"

        delete_status, deleted, _ = _request(
            f"{base_url}/api/v1/admin/model-provider",
            method="DELETE",
            body={"expected_revision": revision, "reason": "remove model configuration"},
            cookie=cookie,
            csrf=csrf,
            request_id="delete-model-1",
        )
        replay_delete_status, replay_deleted, _ = _request(
            f"{base_url}/api/v1/admin/model-provider",
            method="DELETE",
            body={"expected_revision": revision, "reason": "remove model configuration"},
            cookie=cookie,
            csrf=csrf,
            request_id="delete-model-1",
        )
        assert delete_status == replay_delete_status == 200
        assert deleted["model_provider"]["configuration"] is None
        assert replay_deleted["model_provider"]["configuration"] is None

        spec = json.loads(Path("api/openapi/gateway-v1.json").read_text())
        resolver = jsonschema.RefResolver.from_schema(spec)
        for schema_name, payload in (
            ("ModelProviderStatusResponse", public),
            ("ModelProviderDetailResponse", detail),
            ("ModelProviderVerificationResponse", started),
        ):
            jsonschema.Draft202012Validator(
                spec["components"]["schemas"][schema_name],
                resolver=resolver,
            ).validate(payload)
        _, audit, _ = _request(f"{base_url}/api/v1/admin/audit", cookie=cookie)
        assert "secret-provider-key" not in json.dumps(audit)
        assert {item["action"] for item in audit["audit"]} >= {
            "model_provider_update",
            "model_provider_test",
            "model_provider_delete",
        }
    finally:
        gateway_server.shutdown()
        gateway_server.server_close()
        gateway_thread.join(timeout=2)
        diagnosis_server.shutdown()
        diagnosis_server.server_close()
        diagnosis_thread.join(timeout=2)
