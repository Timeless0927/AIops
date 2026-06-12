"""Split Gateway Alertmanager webhook vertical slice tests."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path

import pytest

from aiops.contracts.writeback_auth import WRITEBACK_SECRET_ENV, WRITEBACK_SIGNATURE_HEADER, build_writeback_signature
from apps.aiops_k8s_gateway import alertmanager_webhook as webhook
from apps.aiops_k8s_gateway import main as gateway_main
from toolsets.incident_store import IncidentStore


ROOT = Path(__file__).resolve().parents[1]
BytesReader = io.BytesIO


def _payload(status: str = "firing") -> dict[str, object]:
    return {
        "alerts": [
            {
                "status": status,
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
    old_store = webhook.incident_store._STORE
    monkeypatch.setattr(webhook.incident_store, "_STORE", store)
    try:
        yield store
    finally:
        store.close()
        webhook.incident_store._STORE = old_store


def _post(url: str, payload: dict[str, object]) -> dict[str, object]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as response:
        data = json.loads(response.read().decode("utf-8"))
        assert isinstance(data, dict)
        return data


def _get_signed(url: str, secret: str, path: str) -> dict[str, object]:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            WRITEBACK_SIGNATURE_HEADER: build_writeback_signature(secret, method="GET", path=path, body=b""),
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=5) as response:
        data = json.loads(response.read().decode("utf-8"))
        assert isinstance(data, dict)
        return data


def _wait_for_json(url: str) -> dict[str, object]:
    deadline = time.monotonic() + 10
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                data = json.loads(response.read().decode("utf-8"))
                assert isinstance(data, dict)
                return data
        except Exception as exc:  # pragma: no cover - diagnostic wait loop
            last_error = exc
            time.sleep(0.1)
    raise AssertionError(f"{url} did not become ready: {last_error}")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def asyncio_run(awaitable: object) -> object:
    import asyncio

    return asyncio.run(awaitable)


@pytest.mark.asyncio
async def test_gateway_firing_alert_persists_incident_timeline_and_handoff(
    isolated_store: IncidentStore,
    monkeypatch: pytest.MonkeyPatch,
    **_kwargs: object,
) -> None:
    monkeypatch.setenv("AIOPS_HERMES_URL", "http://hermes.local:8082")

    async def _fake_handoff(**kwargs: object) -> dict[str, object]:
        assert kwargs["dedup_key"] == "PodCrashLooping|default|prod-a"
        return {"status": "requested", "response": {"status": "queued"}}

    monkeypatch.setattr(webhook, "trigger_hermes_diagnosis_session", _fake_handoff)

    result = await webhook.process_payload(_payload("firing"))

    assert result["processed"] == 1
    incident_info = result["incidents"][0]
    assert incident_info["dedup_key"] == "PodCrashLooping|default|prod-a"
    assert incident_info["dedup_key_version"] == "v1"
    assert incident_info["hermes_handoff"]["status"] == "requested"

    incident = await webhook.incident_store.get_incident(incident_info["incident_id"])
    timeline = await webhook.incident_store.get_timeline(incident_info["incident_id"])
    assert incident["platform"] == "gateway"
    assert incident["dedup_key"] == "PodCrashLooping|default|prod-a"
    assert [event["event_type"] for event in timeline] == [
        "alert_fired",
        "hermes_handoff_requested",
    ]
    assert timeline[0]["metadata"]["ingress"] == "split_gateway"
    assert timeline[0]["metadata"]["session_id"] == incident_info["session_id"]


@pytest.mark.asyncio
async def test_gateway_reuses_incident_by_dedup_key(
    isolated_store: IncidentStore,
    monkeypatch: pytest.MonkeyPatch,
    **_kwargs: object,
) -> None:
    async def _fake_handoff(**_: object) -> dict[str, object]:
        return {"status": "skipped", "reason": "test"}

    monkeypatch.setattr(webhook, "trigger_hermes_diagnosis_session", _fake_handoff)

    first = await webhook.process_payload(_payload("firing"))
    second = await webhook.process_payload(_payload("firing"))

    first_incident = first["incidents"][0]["incident_id"]
    second_incident = second["incidents"][0]["incident_id"]
    assert second_incident == first_incident
    timeline = await webhook.incident_store.get_timeline(first_incident)
    assert [event["event_type"] for event in timeline] == [
        "alert_fired",
        "hermes_handoff_skipped",
        "alert_fired",
        "hermes_handoff_skipped",
    ]


@pytest.mark.asyncio
async def test_gateway_resolved_alert_updates_existing_incident(
    isolated_store: IncidentStore,
    monkeypatch: pytest.MonkeyPatch,
    **_kwargs: object,
) -> None:
    async def _fake_handoff(**_: object) -> dict[str, object]:
        return {"status": "skipped", "reason": "test"}

    monkeypatch.setattr(webhook, "trigger_hermes_diagnosis_session", _fake_handoff)
    firing = await webhook.process_payload(_payload("firing"))
    incident_id = firing["incidents"][0]["incident_id"]

    resolved = await webhook.process_payload(_payload("resolved"))

    incident = await webhook.incident_store.get_incident(incident_id)
    timeline = await webhook.incident_store.get_timeline(incident_id)
    assert resolved["processed"] == 1
    assert resolved["incidents"][0]["event_type"] == "resolved"
    assert incident["status"] == "resolved"
    assert timeline[-1]["event_type"] == "resolved"


@pytest.mark.asyncio
async def test_gateway_refiring_resolved_incident_reopens_and_handoffs(
    isolated_store: IncidentStore,
    monkeypatch: pytest.MonkeyPatch,
    **_kwargs: object,
) -> None:
    handoff_sessions: list[str] = []

    async def _fake_handoff(**kwargs: object) -> dict[str, object]:
        handoff_sessions.append(str(kwargs["session_id"]))
        return {"status": "requested", "response": {"status": "queued"}}

    monkeypatch.setattr(webhook, "trigger_hermes_diagnosis_session", _fake_handoff)

    first = await webhook.process_payload(_payload("firing"))
    incident_id = first["incidents"][0]["incident_id"]
    await webhook.process_payload(_payload("resolved"))
    refire = await webhook.process_payload(_payload("firing"))

    incident = await webhook.incident_store.get_incident(incident_id)
    timeline = await webhook.incident_store.get_timeline(incident_id)

    assert refire["processed"] == 1
    assert refire["incidents"][0]["incident_id"] == incident_id
    assert refire["incidents"][0]["reused"] is True
    assert refire["incidents"][0]["reopened"] is True
    assert incident["status"] == "triaging"
    assert incident["reopen_count"] == 1
    assert [event["event_type"] for event in timeline] == [
        "alert_fired",
        "hermes_handoff_requested",
        "resolved",
        "reopened",
        "alert_fired",
        "hermes_handoff_requested",
    ]
    assert len(handoff_sessions) == 2
    assert handoff_sessions[0] != handoff_sessions[1]


def test_gateway_rejects_invalid_payload_and_hmac(
    isolated_store: IncidentStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALERTMANAGER_WEBHOOK_SECRET", "top-secret")
    body = json.dumps(_payload()).encode("utf-8")
    invalid_body = json.dumps({"alerts": {}}).encode("utf-8")
    good_sig = hmac.new(b"top-secret", body, hashlib.sha256).hexdigest()
    invalid_sig = hmac.new(b"top-secret", invalid_body, hashlib.sha256).hexdigest()

    bad_status, bad_result = webhook.handle_http_request(body, {"X-Signature": "bad"})
    invalid_status, invalid_result = webhook.handle_http_request(
        invalid_body,
        {"X-Signature": "sha256=" + invalid_sig},
    )
    ok_status, ok_result = webhook.handle_http_request(body, {"X-Signature": "sha256=" + good_sig})

    assert bad_status == 401
    assert bad_result["ok"] is False
    assert invalid_status == 400
    assert invalid_result["ok"] is False
    assert ok_status == 200
    assert ok_result["processed"] == 1


def test_gateway_accepts_lowercase_hmac_header(
    isolated_store: IncidentStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALERTMANAGER_WEBHOOK_SECRET", "top-secret")
    body = json.dumps(_payload()).encode("utf-8")
    good_sig = hmac.new(b"top-secret", body, hashlib.sha256).hexdigest()

    status, result = webhook.handle_http_request(body, {"x-signature": "sha256=" + good_sig})

    assert status == 200
    assert result["processed"] == 1


@pytest.mark.asyncio
async def test_gateway_diagnosis_writeback_route_and_incident_view(
    isolated_store: IncidentStore,
    monkeypatch: pytest.MonkeyPatch,
    **_: object,
) -> None:
    del monkeypatch
    incident_id = await webhook.incident_store.create_incident(
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


def test_gateway_writeback_http_requires_signature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = IncidentStore(tmp_path / "incidents.db")
    old_store = webhook.incident_store._STORE
    monkeypatch.setattr(webhook.incident_store, "_STORE", store)
    monkeypatch.setenv(WRITEBACK_SECRET_ENV, "writeback-secret")
    try:
        incident_id = asyncio_run(
            webhook.incident_store.create_incident(
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
        handler.path = "/diagnosis/writeback"
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = BytesReader(body)
        writes: list[tuple[int, dict[str, object]]] = []
        handler.write_json = lambda status, result: writes.append((status, result))  # type: ignore[method-assign]

        handler.do_POST()

        assert writes[0][0] == HTTPStatus.UNAUTHORIZED
        stored = asyncio_run(webhook.incident_store.get_incident(str(incident_id)))
        assert stored["diagnosis_json"] is None
    finally:
        store.close()
        webhook.incident_store._STORE = old_store


def test_gateway_writeback_http_accepts_valid_signature_and_protects_incident_view(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "writeback-secret"
    store = IncidentStore(tmp_path / "incidents.db")
    old_store = webhook.incident_store._STORE
    monkeypatch.setattr(webhook.incident_store, "_STORE", store)
    monkeypatch.setenv(WRITEBACK_SECRET_ENV, secret)
    try:
        incident_id = asyncio_run(
            webhook.incident_store.create_incident(
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
        post_handler.path = "/diagnosis/writeback"
        post_handler.headers = {
            "Content-Length": str(len(body)),
            WRITEBACK_SIGNATURE_HEADER: build_writeback_signature(
                secret,
                method="POST",
                path="/diagnosis/writeback",
                body=body,
            ),
        }
        post_handler.rfile = BytesReader(body)
        post_handler.write_json = lambda status, result: post_writes.append((status, result))  # type: ignore[method-assign]

        post_handler.do_POST()

        assert post_writes[0][0] == HTTPStatus.OK

        unsigned_view_writes: list[tuple[int, dict[str, object]]] = []
        unsigned_view = object.__new__(gateway_main.GatewayHandler)
        unsigned_view.path = f"/incidents/{incident_id}"
        unsigned_view.headers = {}
        unsigned_view.write_json = lambda status, result: unsigned_view_writes.append((status, result))  # type: ignore[method-assign]
        unsigned_view.do_GET()

        assert unsigned_view_writes[0][0] == HTTPStatus.UNAUTHORIZED

        signed_view_writes: list[tuple[int, dict[str, object]]] = []
        signed_view = object.__new__(gateway_main.GatewayHandler)
        signed_view.path = f"/incidents/{incident_id}"
        signed_view.headers = {
            WRITEBACK_SIGNATURE_HEADER: build_writeback_signature(
                secret,
                method="GET",
                path=f"/incidents/{incident_id}",
            )
        }
        signed_view.write_json = lambda status, result: signed_view_writes.append((status, result))  # type: ignore[method-assign]
        signed_view.do_GET()

        assert signed_view_writes[0][0] == HTTPStatus.OK
        assert signed_view_writes[0][1]["incident"]["diagnosis"]["summary"] == payload["diagnosis"]["summary"]
    finally:
        store.close()
        webhook.incident_store._STORE = old_store


def test_gateway_http_route_triggers_hermes_boundary(tmp_path: Path) -> None:
    gateway_port = _free_port()
    hermes_port = _free_port()
    writeback_secret = "writeback-secret"
    env = os.environ.copy()
    env.update(
        {
            "AIOPS_DATA_DIR": str(tmp_path / "data"),
            "AIOPS_GATEWAY_HOST": "127.0.0.1",
            "AIOPS_GATEWAY_PORT": str(gateway_port),
            "AIOPS_HERMES_HOST": "127.0.0.1",
            "AIOPS_HERMES_PORT": str(hermes_port),
            "AIOPS_HERMES_URL": f"http://127.0.0.1:{hermes_port}",
            "AIOPS_GATEWAY_URL": f"http://127.0.0.1:{gateway_port}",
            WRITEBACK_SECRET_ENV: writeback_secret,
        }
    )
    hermes = subprocess.Popen(
        [sys.executable, "-m", "hermes.service_main", "--host", "127.0.0.1", "--port", str(hermes_port)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    gateway = subprocess.Popen(
        [sys.executable, "-m", "apps.aiops_k8s_gateway.main", "--host", "127.0.0.1", "--port", str(gateway_port)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert _wait_for_json(f"http://127.0.0.1:{hermes_port}/healthz")["status"] == "ok"
        assert _wait_for_json(f"http://127.0.0.1:{gateway_port}/healthz")["status"] == "ok"

        data = _post(f"http://127.0.0.1:{gateway_port}/webhooks/alertmanager", _payload("firing"))
        incident_id = data["incidents"][0]["incident_id"]
        session_id = data["incidents"][0]["session_id"]
        diagnosis = _wait_for_json(f"http://127.0.0.1:{hermes_port}/diagnosis/sessions/{session_id}/diagnosis")
        incident_view = _get_signed(
            f"http://127.0.0.1:{gateway_port}/incidents/{incident_id}",
            writeback_secret,
            f"/incidents/{incident_id}",
        )
    finally:
        for process in (gateway, hermes):
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

    assert data["processed"] == 1
    handoff = data["incidents"][0]["hermes_handoff"]
    assert handoff["status"] == "requested"
    assert handoff["response"]["status"] == "queued"
    assert handoff["response"]["session"]["status"] == "queued"
    assert diagnosis["session"]["markdown"].startswith("# Incident diagnosis:")
    assert incident_view["incident"]["diagnosis"]["summary"] == diagnosis["session"]["summary"]
    assert incident_view["incident"]["diagnosis_markdown"] == diagnosis["session"]["markdown"]
    writeback_events = [event for event in incident_view["timeline"] if event["event_type"] == "investigate_end"]
    assert writeback_events[-1]["metadata"]["writeback"]["status"] == "succeeded"
    assert any(
        action["approval_required"] is True
        for action in diagnosis["session"]["recommended_actions"]
    )
