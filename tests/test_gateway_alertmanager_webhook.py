"""Split Gateway Alertmanager webhook vertical slice tests."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from apps.aiops_k8s_gateway import alertmanager_webhook as webhook
from apps.aiops_k8s_gateway.connector_identity import ConnectorIdentity
from apps.aiops_k8s_gateway.incident import IncidentService
from apps.aiops_k8s_gateway.resource_catalog import ResourceCatalog
from apps.aiops_k8s_gateway.v1_store import GatewayV1Store


def _payload(status: str = "firing") -> dict[str, object]:
    return {
        "alerts": [
            {
                "status": status,
                "fingerprint": "fp-pod-crash",
                "labels": {
                    "alertname": "PodCrashLooping",
                    "severity": "critical",
                    "namespace": "default",
                    "cluster": "prod-a",
                    "pod": "api-123",
                    "deployment": "api",
                },
                "annotations": {"description": "pod restart count is increasing"},
            }
        ]
    }


@pytest.fixture
def v1_incidents(tmp_path: Path) -> IncidentService:
    store = GatewayV1Store(tmp_path / "gateway.db", credential_factory=lambda: "connector-secret")
    _, credential = store.create_connector_enrollment(
        connector_id="connector-prod",
        cluster_id="prod-a",
        actor_id="admin",
        reason="test setup",
        request_id="req-enroll",
    )
    store.register_connector(credential, "connector-prod", "prod-a", request_id="req-register")
    return IncidentService(store.database, ResourceCatalog(store.database), ConnectorIdentity(store.database))


def test_gateway_rejects_invalid_payload_and_hmac(
    v1_incidents: IncidentService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALERTMANAGER_WEBHOOK_SECRET", "top-secret")
    body = json.dumps(_payload()).encode("utf-8")
    invalid_body = json.dumps({"alerts": {}}).encode("utf-8")
    good_sig = hmac.new(b"top-secret", body, hashlib.sha256).hexdigest()
    invalid_sig = hmac.new(b"top-secret", invalid_body, hashlib.sha256).hexdigest()

    bad_status, bad_result = webhook.handle_http_request(body, {"X-Signature": "bad"}, v1_incidents)
    invalid_status, invalid_result = webhook.handle_http_request(
        invalid_body,
        {"X-Signature": "sha256=" + invalid_sig},
        v1_incidents,
    )
    ok_status, ok_result = webhook.handle_http_request(body, {"X-Signature": "sha256=" + good_sig}, v1_incidents)

    assert bad_status == 401
    assert bad_result["ok"] is False
    assert invalid_status == 400
    assert invalid_result["ok"] is False
    assert ok_status == 200
    assert ok_result["processed"] == 1


def test_gateway_alertmanager_bearer_token_fails_closed(
    v1_incidents: IncidentService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIOPS_ALERTMANAGER_WEBHOOK_TOKEN", "alert-token")
    body = json.dumps(_payload()).encode("utf-8")

    missing_status, missing_result = webhook.handle_http_request(body, {}, v1_incidents)
    bad_status, bad_result = webhook.handle_http_request(body, {"Authorization": "Bearer wrong"}, v1_incidents)
    basic_status, basic_result = webhook.handle_http_request(body, {"Authorization": "Basic alert-token"}, v1_incidents)
    ok_status, ok_result = webhook.handle_http_request(body, {"Authorization": "Bearer alert-token"}, v1_incidents)

    assert missing_status == 401
    assert missing_result == {"ok": False, "message": "alertmanager bearer token verification failed"}
    assert bad_status == 401
    assert bad_result["ok"] is False
    assert basic_status == 401
    assert basic_result["ok"] is False
    assert ok_status == 200
    assert ok_result["processed"] == 1


def test_gateway_alertmanager_bearer_token_is_route_contract_when_hmac_secret_exists(
    v1_incidents: IncidentService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIOPS_ALERTMANAGER_WEBHOOK_TOKEN", "alert-token")
    monkeypatch.setenv("ALERTMANAGER_WEBHOOK_SECRET", "top-secret")
    body = json.dumps(_payload()).encode("utf-8")

    status, result = webhook.handle_http_request(body, {"Authorization": "Bearer alert-token"}, v1_incidents)

    assert status == 200
    assert result["processed"] == 1


def test_gateway_accepts_lowercase_hmac_header(
    v1_incidents: IncidentService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALERTMANAGER_WEBHOOK_SECRET", "top-secret")
    body = json.dumps(_payload()).encode("utf-8")
    good_sig = hmac.new(b"top-secret", body, hashlib.sha256).hexdigest()

    status, result = webhook.handle_http_request(body, {"x-signature": "sha256=" + good_sig}, v1_incidents)

    assert status == 200
    assert result["processed"] == 1
