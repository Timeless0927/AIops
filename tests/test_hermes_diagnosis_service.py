"""Hermes diagnosis runtime/export service tests."""

from __future__ import annotations

import json
from http import HTTPStatus
from dataclasses import asdict
from pathlib import Path

import pytest

from aiops.contracts import EvidenceRef, ToolEnvelope
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
