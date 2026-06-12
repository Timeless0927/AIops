"""Hermes diagnosis runtime/export service tests."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from http import HTTPStatus
from pathlib import Path

import pytest

from aiops.contracts import EvidenceRef, ToolEnvelope
from apps.aiops_k8s_gateway import diagnosis_writeback
from hermes import service_main
from toolsets.incident_store import IncidentStore


def _handoff_payload(incident_id: str) -> dict[str, object]:
    return {
        "incident_id": incident_id,
        "session_id": "diagnosis-test-session",
        "source": "alertmanager",
        "dedup_key": "PaymentErrorRateHigh|payments|prod-a",
        "dedup_key_version": "v1",
        "alert": {
            "alertname": "PaymentErrorRateHigh",
            "severity": "critical",
            "namespace": "payments",
            "cluster": "prod-a",
            "service": "payment-api",
            "description": "payment-api 5xx error rate rose and upstream billing timeout is suspected",
            "status": "firing",
        },
    }


@pytest.mark.asyncio
async def test_start_diagnosis_session_generates_exportable_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    **_: object,
) -> None:
    store = IncidentStore(tmp_path / "incidents.db")
    old_store = service_main.incident_store._STORE
    monkeypatch.setattr(service_main.incident_store, "_STORE", store)
    service_main._DIAGNOSIS_SESSIONS.clear()
    try:
        incident_id = await service_main.incident_store.create_incident(
            "PaymentErrorRateHigh",
            "payments",
            "prod-a",
            "payment-api 5xx error rate rose",
            platform="gateway",
            dedup_key="PaymentErrorRateHigh|payments|prod-a",
        )

        status, payload = await service_main.start_diagnosis_session(_handoff_payload(incident_id))

        assert status == HTTPStatus.OK
        assert payload["status"] == "partial"
        session = payload["session"]
        assert session["session_id"] == "diagnosis-test-session"
        assert session["diagnosis"]["markdown"].startswith("# Incident diagnosis:")
        assert session["diagnosis"]["evidence_chain"]
        assert any(step["source_type"] == "topology" for step in session["missing_evidence"])
        assert any(action["approval_required"] is True for action in session["action_proposals"])
        assert all(action["execute_automatically"] is False for action in session["action_proposals"])

        stored = await service_main.incident_store.get_incident(incident_id)
        assert stored["diagnosis_json"]
        assert json.loads(stored["diagnosis_json"])["summary"] == session["diagnosis"]["summary"]
        assert stored["diagnosis_markdown"] == session["diagnosis"]["markdown"]

        timeline = await service_main.incident_store.get_timeline(incident_id)
        event_types = [event["event_type"] for event in timeline]
        assert event_types == ["investigate_start", "investigate_end", "remediate_proposed"]
        assert timeline[1]["metadata"]["status"] == "partial"
        assert timeline[1]["metadata"]["missing_evidence"][0]["source_type"] == "topology"

        exported_session = service_main.get_session_export("diagnosis-test-session")
        exported_diagnosis = service_main.get_session_export("diagnosis-test-session", artifact="diagnosis")
        exported_markdown = service_main.get_session_export("diagnosis-test-session", artifact="markdown")
        assert exported_session == session
        assert exported_diagnosis == session["diagnosis"]
        assert exported_markdown == {
            "session_id": "diagnosis-test-session",
            "incident_id": incident_id,
            "markdown": session["diagnosis"]["markdown"],
        }
    finally:
        store.close()
        service_main.incident_store._STORE = old_store
        service_main._DIAGNOSIS_SESSIONS.clear()


@pytest.mark.asyncio
async def test_split_store_diagnosis_writeback_persists_gateway_incident_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    **_: object,
) -> None:
    gateway_store = IncidentStore(tmp_path / "gateway" / "incidents.db")
    hermes_store = IncidentStore(tmp_path / "hermes" / "incidents.db")
    old_store = service_main.incident_store._STORE
    monkeypatch.setattr(service_main.incident_store, "_STORE", hermes_store)
    monkeypatch.setenv("AIOPS_GATEWAY_URL", "http://gateway.local:8080")
    service_main._DIAGNOSIS_SESSIONS.clear()

    def _fake_gateway_post(target: str, payload: dict[str, object], _timeout: float) -> dict[str, object]:
        assert target == "http://gateway.local:8080/diagnosis/writeback"
        status, result = asyncio_run(diagnosis_writeback.apply_diagnosis_writeback(payload, store=gateway_store))
        assert status == HTTPStatus.OK
        return result

    monkeypatch.setattr(service_main, "_post_json", _fake_gateway_post)
    try:
        incident_id = await gateway_store.create_incident(
            "PaymentErrorRateHigh",
            "payments",
            "prod-a",
            "payment-api 5xx error rate rose",
            platform="gateway",
            dedup_key="PaymentErrorRateHigh|payments|prod-a",
        )

        status, payload = await service_main.start_diagnosis_session(_handoff_payload(incident_id))

        assert status == HTTPStatus.OK
        session = payload["session"]
        assert session["writeback"]["status"] == "succeeded"
        with pytest.raises(ValueError):
            await hermes_store.get_incident(incident_id)

        stored = await gateway_store.get_incident(incident_id)
        assert json.loads(stored["diagnosis_json"])["summary"] == session["diagnosis"]["summary"]
        assert stored["diagnosis_markdown"] == session["diagnosis"]["markdown"]
        assert stored["diagnosis_summary"] == session["diagnosis"]["summary"]
        assert stored["diagnosis_confidence"] == session["diagnosis"]["confidence"]["score"]
        assert stored["diagnosis_level"] == session["diagnosis"]["confidence"]["level"]
        assert stored["diagnosed_at"]

        timeline = await gateway_store.get_timeline(incident_id)
        assert timeline[-1]["event_type"] == "investigate_end"
        assert timeline[-1]["metadata"]["writeback"]["source"] == "gateway_writeback_api"
        assert timeline[-1]["metadata"]["timeline_refs"]["evidence_refs"]
    finally:
        gateway_store.close()
        hermes_store.close()
        service_main.incident_store._STORE = old_store
        service_main._DIAGNOSIS_SESSIONS.clear()


@pytest.mark.asyncio
async def test_gateway_writeback_failure_keeps_session_export_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    **_: object,
) -> None:
    hermes_store = IncidentStore(tmp_path / "hermes" / "incidents.db")
    old_store = service_main.incident_store._STORE
    monkeypatch.setattr(service_main.incident_store, "_STORE", hermes_store)
    monkeypatch.setenv("AIOPS_GATEWAY_URL", "http://gateway.local:8080")
    service_main._DIAGNOSIS_SESSIONS.clear()

    def _failing_gateway_post(*_args: object) -> dict[str, object]:
        raise OSError("gateway unavailable")

    monkeypatch.setattr(service_main, "_post_json", _failing_gateway_post)
    try:
        status, payload = await service_main.start_diagnosis_session(_handoff_payload("gateway-only-incident"))

        assert status == HTTPStatus.OK
        session = payload["session"]
        assert session["diagnosis"]["markdown"].startswith("# Incident diagnosis:")
        assert session["writeback"]["status"] == "failed"
        assert "gateway unavailable" in session["writeback"]["error"]
        assert service_main.get_session_export("diagnosis-test-session")["writeback"]["status"] == "failed"
        assert service_main.get_session_export("diagnosis-test-session", artifact="timeline")["writeback"]["status"] == "failed"
    finally:
        hermes_store.close()
        service_main.incident_store._STORE = old_store
        service_main._DIAGNOSIS_SESSIONS.clear()


def test_diagnosis_get_routes_export_session_artifacts(monkeypatch: pytest.MonkeyPatch) -> None:
    writes: list[tuple[int, dict[str, object]]] = []
    handler = object.__new__(service_main.HermesServiceHandler)
    handler.path = "/diagnosis/sessions/diagnosis-test-session/markdown"
    handler.write_json = lambda status, payload: writes.append((status, payload))  # type: ignore[method-assign]
    handler.write_not_found = lambda: writes.append((404, {"status": "not_found"}))  # type: ignore[method-assign]
    monkeypatch.setattr(
        service_main,
        "get_session_export",
        lambda session_id, artifact=None: {
            "session_id": session_id,
            "incident_id": "incident-1",
            "markdown": "# Incident diagnosis: partial",
        }
        if artifact == "markdown"
        else None,
    )

    handler.do_GET()

    assert writes == [
        (
            HTTPStatus.OK,
            {
                "service": "hermes",
                "status": "ok",
                "session": {
                    "session_id": "diagnosis-test-session",
                    "incident_id": "incident-1",
                    "markdown": "# Incident diagnosis: partial",
                },
            },
        )
    ]


def test_invalid_diagnosis_handoff_returns_bad_request() -> None:
    status, payload = service_main.validate_diagnosis_payload({"session_id": "diagnosis-test"})

    assert status == HTTPStatus.BAD_REQUEST
    assert payload["status"] == "invalid"
    assert "incident_id" in str(payload["error"])


@pytest.mark.asyncio
async def test_post_diagnosis_session_returns_queued_and_runs_in_background(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    **_: object,
) -> None:
    store = IncidentStore(tmp_path / "incidents.db")
    old_store = service_main.incident_store._STORE
    monkeypatch.setattr(service_main.incident_store, "_STORE", store)
    monkeypatch.setattr(service_main, "start_diagnosis_session", _slow_start_diagnosis_session)
    service_main._DIAGNOSIS_SESSIONS.clear()
    try:
        incident_id = await service_main.incident_store.create_incident(
            "PaymentErrorRateHigh",
            "payments",
            "prod-a",
            "payment-api 5xx error rate rose",
            platform="gateway",
            dedup_key="PaymentErrorRateHigh|payments|prod-a",
        )

        status, payload = service_main.enqueue_diagnosis_session(_handoff_payload(incident_id))

        assert status == HTTPStatus.ACCEPTED
        assert payload["status"] == "queued"
        assert payload["session"]["status"] == "queued"
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            exported = service_main.get_session_export("diagnosis-test-session")
            if exported and exported.get("status") == "diagnosed":
                break
            time.sleep(0.02)
        assert service_main.get_session_export("diagnosis-test-session")["status"] == "diagnosed"
    finally:
        store.close()
        service_main.incident_store._STORE = old_store
        service_main._DIAGNOSIS_SESSIONS.clear()


async def _slow_start_diagnosis_session(payload: dict[str, object]) -> tuple[HTTPStatus, dict[str, object]]:
    await asyncio_sleep()
    session = {
        "incident_id": payload["incident_id"],
        "session_id": payload["session_id"],
        "status": "diagnosed",
        "diagnosis": {"summary": "done", "markdown": "# Incident diagnosis: high"},
        "state_transitions": ["running", "diagnosed"],
        "steps": [],
        "missing_evidence": [],
        "action_proposals": [],
    }
    return HTTPStatus.OK, {"service": "hermes", "status": "diagnosed", "session": session}


async def asyncio_sleep() -> None:
    import asyncio

    await asyncio.sleep(0.1)


def asyncio_run(awaitable: object) -> object:
    import asyncio

    return asyncio.run(awaitable)


@pytest.mark.asyncio
async def test_http_tool_adapter_preserves_evidence_refs(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    envelope = ToolEnvelope(
        request_id="req-1",
        tool_name="query_metrics",
        status="succeeded",
        summary="Prometheus evidence returned one series",
        data={"query_digest": "digest-1"},
        evidence_refs=(
            EvidenceRef(
                ref_id="ev_prom_1",
                source="prometheus",
                cluster_id="prod-a",
                namespace="payments",
                query_digest="digest-1",
            ),
        ),
        audit={"status": "succeeded"},
    )
    monkeypatch.setattr(service_main, "_post_json", lambda *_args: asdict(envelope))

    result = await service_main._http_tool_adapter(
        {
            "request_id": "req-1",
            "cluster_id": "prod-a",
            "namespace": "payments",
            "correlation_id": "incident-1",
        },
        url="http://mcp.local/query_metrics",
        tool_name="query_metrics",
        fallback_source="prometheus",
    )

    assert result.status == "succeeded"
    assert result.summary == "Prometheus evidence returned one series"
    assert result.evidence_refs[0].ref_id == "ev_prom_1"
    assert result.evidence_refs[0].query_digest == "digest-1"


@pytest.mark.asyncio
async def test_prometheus_mcp_adapter_uses_iso8601_time_window(
    monkeypatch: pytest.MonkeyPatch,
    **_: object,
) -> None:
    posted: list[dict[str, object]] = []
    monkeypatch.setenv("AIOPS_PROMETHEUS_MCP_URL", "http://mcp-prometheus.local:8083")

    def _fake_post_json(_target: str, payload: dict[str, object], _timeout: float) -> dict[str, object]:
        posted.append(payload)
        return asdict(
            ToolEnvelope(
                request_id=str(payload["request_id"]),
                tool_name="query_metrics",
                status="succeeded",
                summary="Prometheus evidence returned one series",
                data={"query_digest": "digest-1"},
                evidence_refs=(
                    EvidenceRef(
                        ref_id="ev_prom_1",
                        source="prometheus",
                        cluster_id="prod-a",
                        namespace="payments",
                    ),
                ),
                audit={"status": "succeeded"},
            )
        )

    monkeypatch.setattr(service_main, "_post_json", _fake_post_json)

    result = await service_main._metrics_adapter(
        {
            "request_id": "req-iso",
            "cluster_id": "prod-a",
            "namespace": "payments",
            "service": "payment-api",
            "query": "up",
        }
    )

    assert result.status == "succeeded"
    assert posted
    assert posted[0]["start"].endswith("Z")
    assert posted[0]["end"].endswith("Z")
    assert "now" not in posted[0]["start"]
    assert "now" not in posted[0]["end"]


def test_gateway_read_payload_builds_structured_argv_without_shell_split() -> None:
    payload = service_main._gateway_read_payload(
        {
            "request_id": "incident-1:run_k8s_read",
            "cluster_id": "prod-a",
            "namespace": "payments",
            "service": "payment api",
            "reason": "diagnose payment api",
        }
    )

    assert payload["argv"] == ["kubectl", "get", "pods", "-n", "payments", "-l", "app=payment api"]
