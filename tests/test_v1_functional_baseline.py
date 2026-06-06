"""AIO-67 V1 functional baseline for current runnable contracts."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from aiops.contracts import ErrorCode
from aiops.k8s import CommandEnvelope, ResultEnvelope
from apps.aiops_k8s_gateway.connector_router import ConnectorRoute
from apps.cluster_connector.kubectl_executor import validate_command_envelope
from apps.mcp_loki.facade import query_logs
from apps.mcp_prometheus.facade import query_metrics
from apps.mcp_topology.facade import get_service_topology
from toolsets.incident_diagnosis import build_diagnosis
from toolsets.k8s_read import run_k8s_read
from toolsets.loki_query import LokiBackendError
from toolsets.prometheus_query import PrometheusBackendError
from toolsets.topology_store import ServiceEdge, ServiceRecord, TopologyStore


class FakePrometheusRunner:
    def __init__(self, results: list[dict[str, Any]] | None = None, error: Exception | None = None) -> None:
        self.results = results or []
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def query_range(self, query: str, start: str, end: str, step: str) -> list[dict[str, Any]]:
        self.calls.append({"query": query, "start": start, "end": end, "step": step})
        if self.error:
            raise self.error
        return self.results


class FakeLokiRunner:
    def __init__(self, results: list[dict[str, Any]] | None = None, error: Exception | None = None) -> None:
        self.results = results or []
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def query_range(self, query: str, start: str, end: str, limit: int) -> list[dict[str, Any]]:
        self.calls.append({"query": query, "start": start, "end": end, "limit": limit})
        if self.error:
            raise self.error
        return self.results


def _time_range() -> dict[str, str]:
    return {
        "type": "absolute",
        "value": "2026-06-04T00:00:00Z/2026-06-04T00:10:00Z",
    }


def _metric_series(app: str, value: str) -> list[dict[str, Any]]:
    return [
        {
            "metric": {"app": app, "status": "5xx"},
            "values": [["1780531200", value], ["1780531800", value]],
        }
    ]


def _log_stream(app: str, lines: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "stream": {"app": app, "namespace": "payments"},
            "values": [[str(1780531200000000000 + index), line] for index, line in enumerate(lines)],
        }
    ]


def _command_envelope(**overrides: Any) -> CommandEnvelope:
    payload: dict[str, Any] = {
        "envelope_version": "v1",
        "task_id": "task-67",
        "command_id": "cmd-67",
        "cluster_id": "prod-a",
        "namespace": "payments",
        "action_type": "read",
        "argv": ("kubectl", "get", "pods", "-n", "payments"),
        "timeout_seconds": 15,
        "output_limit_bytes": 262144,
        "risk_level": "low",
        "grant_id": "grant-read-67",
        "reason": "AIO-67 V1 baseline",
    }
    payload.update(overrides)
    return CommandEnvelope(**payload)


def _as_diagnosis_evidence(source_type: str, envelope_ref: Any, summary: str, confidence: float) -> dict[str, Any]:
    return {
        "source_type": source_type,
        "source_ref": envelope_ref.ref_id,
        "summary": summary,
        "confidence": confidence,
        "payload": asdict(envelope_ref),
    }


def test_hermes_gateway_k8s_read_contract_success_and_auth_failure() -> None:
    """Hermes-facing run_k8s_read returns stable V1 envelopes for success and auth failure."""

    async def _execution(argv: list[str], timeout_seconds: int, output_limit_bytes: int) -> dict[str, Any]:
        assert argv == ["kubectl", "get", "pods", "-n", "payments"]
        assert timeout_seconds == 15
        assert output_limit_bytes == 262144
        return {
            "ok": True,
            "exit_code": 0,
            "stdout": "NAME READY\npayment-api-7f 1/1\n",
            "stderr": "",
            "executed_command": argv,
            "truncated": False,
            "error_code": None,
        }

    hermes_request = {
        "request_id": "req-hermes-1",
        "correlation_id": "corr-spike-payment",
        "cluster_id": "prod-a",
        "namespace": "payments",
        "argv": ["kubectl", "get", "pods", "-n", "payments"],
        "reason": "diagnose payment-api error-rate spike",
        "task_id": "task-hermes-1",
        "command_id": "cmd-hermes-1",
        "actor_id": "hermes-agent",
        "operator_profile": {
            "name": "SRE Bot",
            "namespaces": ["payments"],
            "allowed_tools": ["run_k8s_read"],
        },
    }

    with patch("toolsets.k8s_read._run_kubectl_argv", new=AsyncMock(side_effect=_execution)), patch(
        "toolsets.k8s_read.audit_log.record_audit",
        new=AsyncMock(return_value="audit-67"),
    ):
        success = asyncio.run(run_k8s_read(**hermes_request))

    denied = asyncio.run(
        run_k8s_read(
            **{
                **hermes_request,
                "operator_profile": {
                    "name": "No Access",
                    "namespaces": ["payments"],
                    "allowed_tools": ["query_logs"],
                },
            }
        )
    )

    assert success["envelope_version"] == "result.envelope.v1"
    assert success["status"] == "succeeded"
    assert success["stdout"].startswith("NAME READY")
    assert success["audit_ref"] == "audit-67"
    assert success["error"] is None
    assert denied["envelope_version"] == "result.envelope.v1"
    assert denied["status"] == "failed"
    assert denied["error"]["code"] == "permission_denied"


def test_gateway_connector_contract_accepts_result_and_degrades_when_connector_offline() -> None:
    route = ConnectorRoute(cluster_id="prod-a", connector_id="connector-prod-a", session_id="session-1")
    envelope = _command_envelope()

    validate_command_envelope(envelope, connector_cluster_id=route.cluster_id, allowed_namespaces={"payments"})
    result = ResultEnvelope(
        envelope_version="v1",
        task_id=envelope.task_id,
        command_id=envelope.command_id,
        connector_id=route.connector_id,
        cluster_id=route.cluster_id,
        status="succeeded",
        stdout="NAME READY\npayment-api 1/1\n",
        exit_code=0,
    )

    offline = ResultEnvelope(
        envelope_version="v1",
        task_id=envelope.task_id,
        command_id=envelope.command_id,
        connector_id=route.connector_id,
        cluster_id=route.cluster_id,
        status="failed",
        error_code=ErrorCode.CONNECTOR_OFFLINE.value,
        error_message="connector stream is offline",
    )

    assert CommandEnvelope.from_dict(envelope.to_dict()) == envelope
    assert ResultEnvelope.from_dict(result.to_dict()) == result
    assert offline.error_code == ErrorCode.CONNECTOR_OFFLINE.value

    with pytest.raises(ValueError, match="namespace_out_of_scope"):
        validate_command_envelope(
            _command_envelope(namespace="kube-system", argv=("kubectl", "get", "pods", "-n", "kube-system")),
            connector_cluster_id=route.cluster_id,
            allowed_namespaces={"payments"},
        )


def test_gateway_connector_and_read_facades_return_controlled_failure_envelopes() -> None:
    metrics = asyncio.run(query_metrics(
        {
            "request_id": "req-prom-fail",
            "correlation_id": "corr-fail",
            "cluster_id": "prod-a",
            "namespace": "payments",
            "service": "payment-api",
            "query": 'sum(rate(http_requests_total{app="payment-api",status=~"5.."}[5m]))',
            "start": "2026-06-04T00:00:00Z",
            "end": "2026-06-04T00:10:00Z",
            "reason": "baseline failure coverage",
        },
        runner=FakePrometheusRunner(error=PrometheusBackendError("prometheus unavailable")),
    ))
    logs = asyncio.run(query_logs(
        {
            "request_id": "req-loki-fail",
            "correlation_id": "corr-fail",
            "cluster_id": "prod-a",
            "namespace": "payments",
            "service": "payment-api",
            "query": '{app="payment-api"}',
            "time_range": _time_range(),
            "reason": "baseline failure coverage",
            "max_lines": 10,
        },
        runner=FakeLokiRunner(error=LokiBackendError("loki unavailable")),
    ))
    empty_metrics = asyncio.run(query_metrics(
        {
            "request_id": "req-prom-empty",
            "correlation_id": "corr-empty",
            "cluster_id": "prod-a",
            "namespace": "payments",
            "service": "payment-api",
            "query": 'sum(rate(http_requests_total{app="payment-api"}[5m]))',
            "start": "2026-06-04T00:00:00Z",
            "end": "2026-06-04T00:10:00Z",
            "reason": "baseline empty result coverage",
        },
        runner=FakePrometheusRunner([]),
    ))
    missing_topology = get_service_topology(
        {
            "request_id": "req-topology-missing",
            "correlation_id": "corr-empty",
            "cluster_id": "prod-a",
            "namespace": "payments",
            "service": "missing-api",
        }
    )
    with patch("toolsets.k8s_read._run_kubectl_argv", new=AsyncMock(return_value={
        "ok": False,
        "exit_code": -1,
        "stdout": "",
        "stderr": "kubectl 启动失败",
        "executed_command": ["kubectl", "get", "pods", "-n", "payments"],
        "truncated": False,
        "error_code": "backend_unavailable",
    })), patch("toolsets.k8s_read.audit_log.record_audit", new=AsyncMock(return_value="audit-fail")):
        k8s_empty = asyncio.run(run_k8s_read(
            cluster_id="prod-a",
            namespace="payments",
            argv=["kubectl", "get", "pods", "-n", "payments"],
            reason="baseline empty read",
            task_id="task-empty",
            command_id="cmd-empty",
            actor_id="hermes-agent",
        ))

    assert metrics.status == "failed"
    assert metrics.errors[0].code == ErrorCode.BACKEND_UNAVAILABLE
    assert logs.status == "failed"
    assert logs.errors[0].code == ErrorCode.BACKEND_UNAVAILABLE
    assert empty_metrics.status == "succeeded"
    assert empty_metrics.data["series_count"] == 0
    assert missing_topology.status == "partial"
    assert missing_topology.errors[0].code == ErrorCode.SERVICE_NOT_FOUND
    assert "service_not_found" in missing_topology.data["warnings"]
    assert k8s_empty["status"] == "failed"
    assert k8s_empty["error"]["code"] == "backend_unavailable"


def test_payment_api_error_rate_spike_produces_structured_diagnosis(tmp_path: Path) -> None:
    db_path = tmp_path / "topology.db"
    with TopologyStore(db_path) as store:
        store.upsert_service(ServiceRecord("prod-a", "payments", "payment-api", workload_kind="Deployment"))
        store.upsert_service(ServiceRecord("prod-a", "payments", "billing-api", workload_kind="Deployment"))
        store.add_edge(
            ServiceEdge(
                "prod-a",
                "payments",
                "payment-api",
                "prod-a",
                "payments",
                "billing-api",
                "depends_on",
                "manual",
                0.9,
            )
        )

    metrics = asyncio.run(query_metrics(
        {
            "request_id": "req-payment-metrics",
            "correlation_id": "corr-payment-spike",
            "cluster_id": "prod-a",
            "namespace": "payments",
            "service": "payment-api",
            "query": 'sum(rate(http_requests_total{app="payment-api",status=~"5.."}[5m]))',
            "start": "2026-06-04T00:00:00Z",
            "end": "2026-06-04T00:10:00Z",
            "step": "60s",
            "reason": "payment-api error-rate spike",
        },
        runner=FakePrometheusRunner(_metric_series("payment-api", "8")),
    ))
    logs = asyncio.run(query_logs(
        {
            "request_id": "req-payment-logs",
            "correlation_id": "corr-payment-spike",
            "cluster_id": "prod-a",
            "namespace": "payments",
            "service": "payment-api",
            "query": '{app="payment-api"}',
            "time_range": _time_range(),
            "reason": "payment-api error-rate spike",
            "max_lines": 10,
            "sample_size": 2,
        },
        runner=FakeLokiRunner(_log_stream("payment-api", ["error billing-api timeout", "error checkout 502"])),
    ))
    topology = get_service_topology(
        {
            "request_id": "req-payment-topology",
            "correlation_id": "corr-payment-spike",
            "cluster_id": "prod-a",
            "namespace": "payments",
            "service": "payment-api",
        },
        db_path=db_path,
    )

    with patch("toolsets.k8s_read._run_kubectl_argv", new=AsyncMock(return_value={
        "ok": True,
        "exit_code": 0,
        "stdout": "NAME READY\npayment-api 2/2\n",
        "stderr": "",
        "executed_command": ["kubectl", "get", "pods", "-n", "payments"],
        "truncated": False,
        "error_code": None,
    })), patch("toolsets.k8s_read.audit_log.record_audit", new=AsyncMock(return_value="audit-payment")):
        k8s = asyncio.run(run_k8s_read(
            cluster_id="prod-a",
            namespace="payments",
            argv=["kubectl", "get", "pods", "-n", "payments"],
            reason="payment-api error-rate spike",
            task_id="task-payment-read",
            command_id="cmd-payment-read",
            actor_id="hermes-agent",
        ))

    diagnosis = build_diagnosis(
        incident={"alert_name": "PaymentErrorRateHigh", "namespace": "payments", "cluster": "prod-a"},
        evidence_refs=[
            _as_diagnosis_evidence("metrics", metrics.evidence_refs[0], "payment-api 5xx error rate rose to 8%", 0.9),
            _as_diagnosis_evidence("logs", logs.evidence_refs[0], "payment-api logs show billing-api timeout", 0.85),
            _as_diagnosis_evidence("topology", topology.evidence_refs[0], "payment-api depends on billing-api", 0.8),
            {
                "source_type": "k8s_read",
                "source_ref": k8s["audit_ref"],
                "summary": "payment-api pods are ready; failure likely upstream or application-level",
                "confidence": 0.7,
            },
        ],
        recommended_actions=[
            {"summary": "Query billing-api latency and error metrics", "action_type": "read"},
            {"summary": "Rollback payment-api deployment if a regression is confirmed", "action_type": "k8s_write"},
        ],
    )

    assert metrics.status == "succeeded"
    assert logs.status == "succeeded"
    assert topology.status == "succeeded"
    assert topology.evidence_refs[0].source == "topology"
    assert k8s["status"] == "succeeded"
    assert diagnosis["confidence"]["level"] == "high"
    assert len(diagnosis["evidence_chain"]) == 4
    assert diagnosis["root_cause_candidates"]
    assert diagnosis["recommended_actions"][0]["approval_required"] is False
    assert diagnosis["recommended_actions"][1]["approval_required"] is True
    assert diagnosis["recommended_actions"][1]["execute_automatically"] is False


def test_pod_crashloop_spike_uses_k8s_logs_and_requires_approval_for_mutation_advice() -> None:
    async def _execution(argv: list[str], *_args: Any) -> dict[str, Any]:
        if argv[:3] == ["kubectl", "describe", "pod/payment-api-7f"]:
            stdout = "State: Waiting\nReason: CrashLoopBackOff\nLast State: Terminated\nExit Code: 1\n"
        else:
            stdout = "panic: missing DATABASE_URL\nBack-off restarting failed container\n"
        return {
            "ok": True,
            "exit_code": 0,
            "stdout": stdout,
            "stderr": "",
            "executed_command": argv,
            "truncated": False,
            "error_code": None,
        }

    with patch("toolsets.k8s_read._run_kubectl_argv", new=AsyncMock(side_effect=_execution)), patch(
        "toolsets.k8s_read.audit_log.record_audit",
        new=AsyncMock(return_value="audit-crashloop"),
    ):
        describe = asyncio.run(run_k8s_read(
            cluster_id="prod-a",
            namespace="payments",
            argv=["kubectl", "describe", "pod/payment-api-7f", "-n", "payments"],
            reason="Pod CrashLoopBackOff spike",
            task_id="task-crash-describe",
            command_id="cmd-crash-describe",
            actor_id="hermes-agent",
        ))
        logs = asyncio.run(run_k8s_read(
            cluster_id="prod-a",
            namespace="payments",
            argv=["kubectl", "logs", "pod/payment-api-7f", "-n", "payments", "--previous"],
            reason="Pod CrashLoopBackOff spike",
            task_id="task-crash-logs",
            command_id="cmd-crash-logs",
            actor_id="hermes-agent",
        ))

    loki = asyncio.run(query_logs(
        {
            "request_id": "req-crash-loki",
            "correlation_id": "corr-crashloop-spike",
            "cluster_id": "prod-a",
            "namespace": "payments",
            "service": "payment-api",
            "query": '{app="payment-api"}',
            "time_range": _time_range(),
            "reason": "Pod CrashLoopBackOff spike",
            "max_lines": 10,
        },
        runner=FakeLokiRunner(_log_stream("payment-api", ["error CrashLoopBackOff missing DATABASE_URL"])),
    ))
    diagnosis = build_diagnosis(
        incident={"alert_name": "PodCrashLoopBackOff", "namespace": "payments", "cluster": "prod-a"},
        evidence_refs=[
            {
                "source_type": "k8s_read",
                "source_ref": describe["audit_ref"],
                "summary": "Pod is in CrashLoopBackOff with exit code 1",
                "confidence": 0.85,
            },
            {
                "source_type": "k8s_read",
                "source_ref": logs["audit_ref"],
                "summary": "Previous container log shows missing DATABASE_URL before restart",
                "confidence": 0.8,
            },
            _as_diagnosis_evidence("logs", loki.evidence_refs[0], "Loki confirms CrashLoopBackOff error pattern", 0.75),
        ],
        recommended_actions=[
            {"summary": "Read deployment environment and config references", "action_type": "read"},
            {"summary": "Patch deployment env after human approval", "action_type": "mutation"},
        ],
    )

    assert describe["status"] == "succeeded"
    assert logs["status"] == "succeeded"
    assert loki.status == "succeeded"
    assert diagnosis["confidence"]["level"] == "high"
    assert "workload crash loop" in diagnosis["root_cause_candidates"][0]["cause"]
    assert diagnosis["recommended_actions"][0]["approval_required"] is False
    assert diagnosis["recommended_actions"][1]["approval_required"] is True
    assert diagnosis["recommended_actions"][1]["execute_automatically"] is False
