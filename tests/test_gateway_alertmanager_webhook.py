"""Split Gateway Alertmanager webhook vertical slice tests."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
from http import HTTPStatus
from pathlib import Path

import pytest

from apps.aiops_k8s_gateway import alertmanager_webhook as webhook
from apps.aiops_k8s_gateway import main as gateway_main
from apps.aiops_k8s_gateway import notification_center
from apps.aiops_k8s_gateway.connector_identity import ConnectorIdentity
from apps.aiops_k8s_gateway.incident import IncidentService
from apps.aiops_k8s_gateway.resource_catalog import ResourceCatalog
from apps.aiops_k8s_gateway.v1_store import GatewayV1Store
from toolsets import incident_store as legacy_incident_store
from toolsets.incident_store import IncidentStore


BytesReader = io.BytesIO


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
def isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> IncidentStore:
    store = IncidentStore(tmp_path / "incidents.db")
    old_store = legacy_incident_store._STORE
    monkeypatch.setattr(legacy_incident_store, "_STORE", store)
    try:
        yield store
    finally:
        store.close()
        legacy_incident_store._STORE = old_store


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


def asyncio_run(awaitable: object) -> object:
    import asyncio

    return asyncio.run(awaitable)


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


@pytest.mark.asyncio
async def test_gateway_diagnosis_writeback_route_and_incident_view(
    isolated_store: IncidentStore,
    monkeypatch: pytest.MonkeyPatch,
    **_: object,
) -> None:
    del monkeypatch
    incident_id = await legacy_incident_store.create_incident(
        "PaymentErrorRateHigh",
        "payments",
        "prod-a",
        "payment-api 5xx error rate rose",
        platform="gateway",
        dedup_key="PaymentErrorRateHigh|payments|prod-a",
    )
    writeback_payload = {
        "incident_id": incident_id,
        "session_id": "diagnosis-test-session",
        "status": "partial",
        "diagnosis": {
            "summary": "payment-api 5xx rose with billing timeout evidence",
            "confidence": {"score": 0.82, "level": "high"},
            "evidence_chain": [{"id": "ev-1", "source_type": "metrics", "source_ref": "ev-prom"}],
            "recommended_actions": [{"summary": "read-only verification", "approval_required": False}],
            "markdown": "# Incident diagnosis: high",
        },
        "missing_evidence": [{"source_type": "topology"}],
        "timeline_refs": {"evidence_refs": ["ev-prom"], "state_transitions": ["running", "partial"]},
    }

    status, result = await gateway_main.apply_diagnosis_writeback(writeback_payload)
    view_status, view = await gateway_main.read_incident_view(incident_id)

    assert status == HTTPStatus.OK
    assert result["status"] == "persisted"
    assert view_status == HTTPStatus.OK
    assert view["incident"]["diagnosis"]["summary"] == writeback_payload["diagnosis"]["summary"]
    assert view["incident"]["diagnosis_markdown"] == "# Incident diagnosis: high"
    assert view["timeline"][-1]["event_type"] == "investigate_end"
    assert view["timeline"][-1]["metadata"]["writeback"]["status"] == "succeeded"
    assert view["timeline"][-1]["metadata"]["timeline_refs"]["evidence_refs"] == ["ev-prom"]


async def test_gateway_diagnosis_writeback_needs_human_notifies(
    isolated_store: IncidentStore,
    tmp_path: Path,
    **_: object,
) -> None:
    incident_id = await legacy_incident_store.create_incident(
        "PaymentNeedsHuman",
        "payments",
        "prod-a",
        "payment-api needs human input",
        service="payment-api",
        team="payments",
        platform="gateway",
        dedup_key="PaymentNeedsHuman|payments|prod-a",
    )
    old_center = notification_center._CENTER
    notification_center._CENTER = notification_center.NotificationCenter(
        db=notification_center.NotificationDeliveryDB(tmp_path / "notification_deliveries.db"),
        settings=notification_center.NotificationSettings(
            console_base_url="https://console.example.test",
            max_attempts=1,
            retry_delay_seconds=0,
            channel_config={
                "services": {"payment-api": {"team_id": "payments"}},
                "teams": {"payments": {"feishu_chat_id": "oc_payments"}},
            },
            dry_run=True,
        ),
    )
    try:
        status, result = await gateway_main.apply_diagnosis_writeback(
            {
                "incident_id": incident_id,
                "session_id": "diagnosis-needs-human",
                "status": "needs_human",
                "diagnosis": {
                    "summary": "need human to pick the ambiguous dependency",
                    "confidence": {"score": 0.4, "level": "low"},
                    "markdown": "# Needs human",
                },
            }
        )
        deliveries = notification_center.list_deliveries(notification_type="agent_waiting_for_human_input")
    finally:
        notification_center._CENTER.db.close()
        notification_center._CENTER = old_center

    assert status == HTTPStatus.OK
    assert result["status"] == "persisted"
    assert deliveries[0]["notification_type"] == "agent_waiting_for_human_input"
    assert deliveries[0]["incident_id"] == incident_id
    assert deliveries[0]["card"]["elements"][1]["actions"][0]["url"] == "https://console.example.test/agent-runs/diagnosis-needs-human"


def test_gateway_writeback_http_requires_service_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = IncidentStore(tmp_path / "incidents.db")
    old_store = legacy_incident_store._STORE
    monkeypatch.setattr(legacy_incident_store, "_STORE", store)
    def deny(handler, **_kwargs):
        handler.write_json(HTTPStatus.UNAUTHORIZED, {"status": "unauthorized"})
        return None

    monkeypatch.setattr(gateway_main, "enforce_internal_auth", deny)
    try:
        incident_id = asyncio_run(
            legacy_incident_store.create_incident(
                "PaymentErrorRateHigh",
                "payments",
                "prod-a",
                "payment-api 5xx error rate rose",
                platform="gateway",
            )
        )
        payload = {
            "incident_id": incident_id,
            "session_id": "diagnosis-test-session",
            "status": "partial",
            "diagnosis": {
                "summary": "forged diagnosis",
                "confidence": {"score": 0.1, "level": "low"},
                "markdown": "# Incident diagnosis: low",
            },
        }
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        handler = object.__new__(gateway_main.GatewayHandler)
        handler.command = "POST"
        handler.path = "/diagnosis/writeback"
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = BytesReader(body)
        writes: list[tuple[int, dict[str, object]]] = []
        handler.write_json = lambda status, result: writes.append((status, result))  # type: ignore[method-assign]

        handler.do_POST()

        assert writes[0][0] == HTTPStatus.UNAUTHORIZED
        stored = asyncio_run(legacy_incident_store.get_incident(str(incident_id)))
        assert stored["diagnosis_json"] is None
    finally:
        store.close()
        legacy_incident_store._STORE = old_store


def test_gateway_writeback_http_accepts_diagnosis_identity_and_protects_incident_view(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = IncidentStore(tmp_path / "incidents.db")
    old_store = legacy_incident_store._STORE
    monkeypatch.setattr(legacy_incident_store, "_STORE", store)
    monkeypatch.setattr(
        gateway_main,
        "enforce_internal_auth",
        lambda *_args, **_kwargs: "system:serviceaccount:aiops-dev:aiops-diagnosis",
    )
    try:
        incident_id = asyncio_run(
            legacy_incident_store.create_incident(
                "PaymentErrorRateHigh",
                "payments",
                "prod-a",
                "payment-api 5xx error rate rose",
                platform="gateway",
            )
        )
        payload = {
            "incident_id": incident_id,
            "session_id": "diagnosis-test-session",
            "status": "partial",
            "diagnosis": {
                "summary": "payment-api 5xx rose with billing timeout evidence",
                "confidence": {"score": 0.82, "level": "high"},
                "evidence_chain": [{"id": "ev-1", "source_type": "metrics", "source_ref": "ev-prom"}],
                "markdown": "# Incident diagnosis: high",
            },
            "timeline_refs": {"evidence_refs": ["ev-prom"], "state_transitions": ["running", "partial"]},
        }
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        post_writes: list[tuple[int, dict[str, object]]] = []
        post_handler = object.__new__(gateway_main.GatewayHandler)
        post_handler.command = "POST"
        post_handler.path = "/diagnosis/writeback"
        post_handler.headers = {"Content-Length": str(len(body)), "Authorization": "Bearer projected-token"}
        post_handler.rfile = BytesReader(body)
        post_handler.write_json = lambda status, result: post_writes.append((status, result))  # type: ignore[method-assign]

        post_handler.do_POST()

        assert post_writes[0][0] == HTTPStatus.OK

        unsigned_view_writes: list[tuple[int, dict[str, object]]] = []
        unsigned_view = object.__new__(gateway_main.GatewayHandler)
        unsigned_view.command = "GET"
        unsigned_view.path = f"/incidents/{incident_id}"
        unsigned_view.headers = {}
        unsigned_view.write_json = lambda status, result: unsigned_view_writes.append((status, result))  # type: ignore[method-assign]
        def deny(handler, **_kwargs):
            handler.write_json(HTTPStatus.UNAUTHORIZED, {"status": "unauthorized"})
            return None

        monkeypatch.setattr(gateway_main, "enforce_internal_auth", deny)
        unsigned_view.do_GET()

        assert unsigned_view_writes[0][0] == HTTPStatus.UNAUTHORIZED

        signed_view_writes: list[tuple[int, dict[str, object]]] = []
        internal_view = object.__new__(gateway_main.GatewayHandler)
        internal_view.command = "GET"
        internal_view.path = f"/incidents/{incident_id}"
        internal_view.headers = {"Authorization": "Bearer projected-token"}
        internal_view.write_json = lambda status, result: signed_view_writes.append((status, result))  # type: ignore[method-assign]
        monkeypatch.setattr(
            gateway_main,
            "enforce_internal_auth",
            lambda *_args, **_kwargs: "system:serviceaccount:aiops-dev:aiops-diagnosis",
        )
        internal_view.do_GET()

        assert signed_view_writes[0][0] == HTTPStatus.OK
        assert signed_view_writes[0][1]["incident"]["diagnosis"]["summary"] == payload["diagnosis"]["summary"]
    finally:
        store.close()
        legacy_incident_store._STORE = old_store
