"""Diagnosis runtime/export service tests."""

from __future__ import annotations

from dataclasses import asdict
from http import HTTPStatus
from pathlib import Path

import pytest

from aiops.contracts import EvidenceRef, ToolEnvelope
from diagnosis_service import service_main
from diagnosis_service.jobs import DiagnosisJobs


def _handoff_payload(incident_id: str) -> dict[str, object]:
    return {
        "request_id": "diagnosis-test-session",
        "incident_id": incident_id,
        "investigation_id": "investigation-1",
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


def test_handoff_preserves_alertmanager_podcrash_target_fields() -> None:
    payload = _handoff_payload("incident-pod-crash")
    alert = payload["alert"]
    assert isinstance(alert, dict)
    alert.update(
        {
            "alertname": "PodCrashLooping",
            "service": "",
            "namespace": "demo-apps",
            "cluster": "dev-external",
            "pod_name": "demo-probe-7d9f4c78df-x2abc",
            "container_name": "demo",
            "workload_kind": "Deployment",
            "workload_name": "demo-probe",
        }
    )

    incident = service_main._incident_from_handoff(payload)

    assert incident["service"] == "demo-probe"
    assert incident["pod_name"] == "demo-probe-7d9f4c78df-x2abc"
    assert incident["container_name"] == "demo"
    assert incident["workload_kind"] == "Deployment"
    assert incident["workload_name"] == "demo-probe"


@pytest.mark.asyncio
async def test_run_diagnosis_job_returns_partial_result_without_process_state(**_: object) -> None:
    session = await service_main.run_diagnosis_job(_handoff_payload("incident-1"))

    assert session["status"] == "needs_human"
    assert session["session_id"] == "diagnosis-test-session"
    assert session["diagnosis"]["markdown"].startswith("# Incident diagnosis:")
    assert session["diagnosis"]["evidence_chain"] == []
    assert any(step["source_type"] == "topology" for step in session["missing_evidence"])
    assert any(action["approval_required"] is True for action in session["action_proposals"])
    assert all(action["execute_automatically"] is False for action in session["action_proposals"])


def test_diagnosis_get_routes_export_persisted_job_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    jobs = DiagnosisJobs(tmp_path / "diagnosis.db", retry_base_seconds=0)
    jobs.accept(_handoff_payload("incident-1"))
    jobs.run_execution_once(
        lambda payload: {
            "session_id": payload["session_id"],
            "incident_id": payload["incident_id"],
            "status": "partial",
            "diagnosis": {"summary": "partial", "markdown": "# Incident diagnosis: partial"},
            "state_transitions": ["running", "partial"],
            "steps": [],
            "missing_evidence": [],
        }
    )
    writes: list[tuple[int, dict[str, object]]] = []
    handler = object.__new__(service_main.DiagnosisServiceHandler)
    handler.path = "/diagnosis/sessions/diagnosis-test-session/markdown"
    handler.headers = {"Authorization": "Bearer projected-token"}
    handler.write_json = lambda status, payload: writes.append((status, payload))  # type: ignore[method-assign]
    handler.write_not_found = lambda: writes.append((404, {"status": "not_found"}))  # type: ignore[method-assign]
    monkeypatch.setattr(service_main, "_diagnosis_jobs", lambda: jobs)
    monkeypatch.setattr(service_main, "enforce_internal_auth", lambda *_args, **_kwargs: "gateway-identity")

    handler.do_GET()

    assert writes == [
        (
            HTTPStatus.OK,
            {
                "service": "diagnosis",
                "status": "ok",
                "session": {
                    "session_id": "diagnosis-test-session",
                    "incident_id": "incident-1",
                    "markdown": "# Incident diagnosis: partial",
                },
            },
        )
    ]


def test_post_diagnosis_session_persists_before_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    jobs = DiagnosisJobs(tmp_path / "diagnosis.db")
    writes: list[tuple[int, dict[str, object]]] = []
    handler = object.__new__(service_main.DiagnosisServiceHandler)
    handler.path = "/diagnosis/sessions"
    handler.headers = {"Authorization": "Bearer projected-token"}
    handler.read_json_body = lambda: _handoff_payload("incident-1")  # type: ignore[method-assign]
    handler.write_json = lambda status, payload: writes.append((status, payload))  # type: ignore[method-assign]
    monkeypatch.setattr(service_main, "_diagnosis_jobs", lambda: jobs)
    monkeypatch.setattr(service_main, "enforce_internal_auth", lambda *_args, **_kwargs: "gateway-identity")

    handler.do_POST()

    assert writes[0][0] == HTTPStatus.ACCEPTED
    assert writes[0][1]["status"] == "accepted"
    assert DiagnosisJobs(tmp_path / "diagnosis.db").get("diagnosis-test-session")["status"] == "queued"  # type: ignore[index]


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
async def test_k8s_read_adapter_uses_gateway_internal_route(
    monkeypatch: pytest.MonkeyPatch,
    **_: object,
) -> None:
    posted: dict[str, object] = {}
    monkeypatch.setenv("AIOPS_GATEWAY_URL", "http://gateway.local:8080")

    def _fake_post_json(
        target: str,
        payload: dict[str, object],
        _timeout: float,
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        posted["target"] = target
        posted["payload"] = payload
        posted["headers"] = headers
        return asdict(
            ToolEnvelope(
                request_id=str(payload.get("request_id") or payload["task_id"]),
                tool_name="run_k8s_read",
                status="succeeded",
                summary="K8s read returned pods",
                data={"items": [{"name": "api"}]},
                audit={"status": "succeeded"},
            )
        )

    monkeypatch.setattr(service_main, "_post_json", _fake_post_json)

    result = await service_main._k8s_read_adapter(
        {
            "request_id": "incident-1:run_k8s_read",
            "cluster_id": "prod-a",
            "namespace": "payments",
            "service": "payment-api",
        }
    )

    assert result.status == "succeeded"
    assert posted["target"] == "http://gateway.local:8080/k8s/read"
    assert posted["headers"] is None


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

    assert payload["argv"] == [
        "kubectl",
        "get",
        "pods",
        "-n",
        "payments",
        "-l",
        "app.kubernetes.io/name=payment api",
    ]


def test_gateway_read_payload_prefers_explicit_selector() -> None:
    payload = service_main._gateway_read_payload(
        {
            "request_id": "incident-1:run_k8s_read",
            "cluster_id": "prod-a",
            "namespace": "payments",
            "service": "payment-api",
            "selector": "app=payment-api",
            "reason": "diagnose payment-api",
        }
    )

    assert payload["argv"] == ["kubectl", "get", "pods", "-n", "payments", "-l", "app=payment-api"]
    assert payload["selector"] == "app=payment-api"


@pytest.mark.asyncio
async def test_topology_mcp_adapter_uses_http_tool_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    **_: object,
) -> None:
    posted: list[tuple[str, dict[str, object], float]] = []
    monkeypatch.setenv("AIOPS_TOPOLOGY_MCP_URL", "http://mcp-topology.local:8085")

    def _fake_post_json(target: str, payload: dict[str, object], timeout: float) -> dict[str, object]:
        posted.append((target, payload, timeout))
        return asdict(
            ToolEnvelope(
                request_id=str(payload["request_id"]),
                tool_name="get_service_topology",
                status="succeeded",
                summary="Topology evidence returned one dependency",
                data={"service": {"found": True}},
                evidence_refs=(
                    EvidenceRef(
                        ref_id="ev_topology_payment",
                        source="topology",
                        cluster_id="prod-a",
                        namespace="payments",
                        service="payment-api",
                    ),
                ),
                audit={"status": "succeeded"},
            )
        )

    monkeypatch.setattr(service_main, "_post_json", _fake_post_json)

    result = await service_main._topology_adapter(
        {
            "request_id": "incident-1:get_service_topology",
            "cluster_id": "prod-a",
            "namespace": "payments",
            "service": "payment-api",
        }
    )

    assert result.status == "succeeded"
    assert result.tool_name == "get_service_topology"
    assert result.evidence_refs[0].ref_id == "ev_topology_payment"
    assert posted[0][0] == "http://mcp-topology.local:8085/get_service_topology"
    assert posted[0][1]["service"] == "payment-api"
