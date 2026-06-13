"""Tests for AIO-51 incident diagnosis runtime skeleton."""

from __future__ import annotations

import json
from typing import Any

import pytest

from aiops.contracts import ErrorCode, EvidenceRef, ToolEnvelope, ToolError
from toolsets.incident_diagnosis import build_diagnosis, run_diagnosis_session, to_json


class FakeIncidentStore:
    def __init__(self) -> None:
        self.recorded: list[tuple[str, dict[str, Any]]] = []

    async def record_incident_diagnosis(self, incident_id: str, diagnosis: dict[str, Any]) -> None:
        self.recorded.append((incident_id, diagnosis))


class FakeAdapter:
    def __init__(self, results: list[ToolEnvelope]) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, args: dict[str, Any]) -> ToolEnvelope:
        self.calls.append(args)
        if not self.results:
            raise AssertionError(f"unexpected adapter call: {args}")
        return self.results.pop(0)


class LabelAwareK8sAdapter:
    def __init__(self, resources: list[dict[str, Any]]) -> None:
        self.resources = resources
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, args: dict[str, Any]) -> ToolEnvelope:
        self.calls.append(args)
        selector = str(args.get("selector") or "")
        key, _, value = selector.partition("=")
        items = [
            resource
            for resource in self.resources
            if resource.get("metadata", {}).get("labels", {}).get(key) == value
        ]
        return _envelope(
            "run_k8s_read",
            summary=f"{selector} returned {len(items)} matching resources",
            source="k8s_gateway",
            ref_id=f"ev_k8s_payment_{len(self.calls)}",
            data={"items": items},
        )


def _envelope(
    tool_name: str,
    *,
    status: str = "succeeded",
    summary: str,
    source: str,
    ref_id: str | None = None,
    data: dict[str, Any] | None = None,
    error_code: ErrorCode | None = None,
) -> ToolEnvelope:
    evidence_refs: tuple[EvidenceRef, ...] = ()
    if ref_id:
        evidence_refs = (
            EvidenceRef(
                ref_id=ref_id,
                source=source,
                cluster_id="prod-a",
                namespace="payments",
                service="payment-api",
            ),
        )
    errors: tuple[ToolError, ...] = ()
    if error_code:
        errors = (ToolError(code=error_code, message=summary),)
    return ToolEnvelope(
        request_id=f"req-{tool_name}",
        correlation_id="corr-incident-1",
        tool_name=tool_name,
        status=status,
        summary=summary,
        data=data or {},
        evidence_refs=evidence_refs,
        audit={"status": status, "tool_name": tool_name, "error_code": error_code.value if error_code else None},
        errors=errors,
    )


@pytest.mark.asyncio
async def test_diagnosis_session_payment_error_rate_succeeds_and_persists() -> None:
    store = FakeIncidentStore()
    metrics = FakeAdapter(
        [
            _envelope(
                "query_metrics",
                summary="payment-api 5xx error rate is 8%",
                source="prometheus",
                ref_id="ev_prom_payment_5xx",
            )
        ]
    )
    logs = FakeAdapter(
        [
            _envelope(
                "query_logs",
                summary="payment-api checkout requests timeout calling billing-api",
                source="loki",
                ref_id="ev_loki_payment_timeout",
            )
        ]
    )
    topology = FakeAdapter(
        [
            _envelope(
                "get_service_topology",
                summary="payment-api depends on billing-api",
                source="topology",
                ref_id="ev_topology_payment",
            )
        ]
    )
    k8s = FakeAdapter(
        [
            _envelope(
                "run_k8s_read",
                summary="deployment/payment-api is available and pods are ready",
                source="k8s_gateway",
                ref_id="ev_k8s_payment_deploy",
            )
        ]
    )

    session = await run_diagnosis_session(
        {
            "incident_id": "incident-1",
            "alert_name": "PaymentErrorRateHigh",
            "namespace": "payments",
            "cluster": "prod-a",
            "service": "payment-api",
            "time_range": "2026-06-04T00:00:00Z/2026-06-04T00:10:00Z",
            "summary": "payment-api 5xx rate elevated",
        },
        metrics_adapter=metrics,
        logs_adapter=logs,
        topology_adapter=topology,
        k8s_read_adapter=k8s,
        incident_store=store,
    )

    assert session["status"] == "diagnosed"
    assert [step["tool"] for step in session["steps"]] == [
        "query_metrics",
        "query_logs",
        "run_k8s_read",
        "get_service_topology",
    ]
    assert all(step["evidence_ref"] or step["missing_reason"] for step in session["steps"])
    assert logs.calls[0]["reason"] == "metrics suggested payment-api 5xx error rate is 8%"
    assert topology.calls[0]["service"] == "payment-api"
    assert store.recorded[0][0] == "incident-1"
    assert session["diagnosis"]["confidence"]["level"] == "high"
    assert session["diagnosis"]["markdown"].startswith("# Incident diagnosis: high")
    assert k8s.calls[0]["argv"] == [
        "kubectl",
        "get",
        "pods",
        "-n",
        "payments",
        "-l",
        "app.kubernetes.io/name=payment-api",
    ]
    assert k8s.calls[0]["selector"] == "app.kubernetes.io/name=payment-api"
    assert k8s.calls[0]["command"] == (
        "kubectl get pods -n payments -l app.kubernetes.io/name=payment-api"
    )


@pytest.mark.asyncio
async def test_diagnosis_session_records_k8s_selector_and_match_count() -> None:
    k8s = FakeAdapter(
        [
            _envelope(
                "run_k8s_read",
                summary="payment-api pod list returned 2 matching resources",
                source="k8s_gateway",
                ref_id="ev_k8s_payment_pods",
                data={
                    "items": [
                        {"metadata": {"name": "payment-api-7f"}},
                        {"metadata": {"name": "payment-api-8g"}},
                    ]
                },
            )
        ]
    )
    logs = FakeAdapter(
        [
            _envelope(
                "query_logs",
                summary="payment-api logs include upstream timeout",
                source="loki",
                ref_id="ev_loki_payment_timeout",
            )
        ]
    )

    session = await run_diagnosis_session(
        {
            "incident_id": "incident-k8s-match-count",
            "alert_name": "PodCrashLoopBackOff",
            "namespace": "payments",
            "cluster": "prod-a",
            "service": "payment-api",
            "summary": "payment-api pod crash loop",
        },
        logs_adapter=logs,
        k8s_read_adapter=k8s,
    )

    k8s_step = session["steps"][0]
    k8s_evidence = session["diagnosis"]["evidence_chain"][0]

    assert k8s_step["status"] == "succeeded"
    assert k8s_step["audit"]["selector"] == "app.kubernetes.io/name=payment-api"
    assert k8s_step["audit"]["resource_match_count"] == 2
    assert k8s_evidence["payload"]["selector"] == "app.kubernetes.io/name=payment-api"
    assert k8s_evidence["payload"]["resource_match_count"] == 2
    assert k8s_evidence["confidence"] == 0.8


@pytest.mark.asyncio
async def test_payment_api_selector_uses_recommended_label_when_legacy_app_label_would_not_match() -> None:
    resources = [
        {
            "metadata": {
                "name": "payment-api-7f",
                "labels": {"app.kubernetes.io/name": "payment-api"},
            }
        }
    ]
    logs = FakeAdapter(
        [
            _envelope(
                "query_logs",
                summary="payment-api logs include upstream timeout",
                source="loki",
                ref_id="ev_loki_payment_timeout",
            )
        ]
    )

    legacy_k8s = LabelAwareK8sAdapter(resources)
    legacy_session = await run_diagnosis_session(
        {
            "incident_id": "incident-k8s-legacy-selector",
            "alert_name": "PodCrashLoopBackOff",
            "namespace": "payments",
            "cluster": "prod-a",
            "service": "payment-api",
            "summary": "payment-api pod crash loop",
            "k8s_selector": "app=payment-api",
        },
        logs_adapter=logs,
        k8s_read_adapter=legacy_k8s,
    )

    recommended_k8s = LabelAwareK8sAdapter(resources)
    recommended_logs = FakeAdapter(
        [
            _envelope(
                "query_logs",
                summary="payment-api logs include upstream timeout",
                source="loki",
                ref_id="ev_loki_payment_timeout",
            )
        ]
    )
    recommended_session = await run_diagnosis_session(
        {
            "incident_id": "incident-k8s-recommended-selector",
            "alert_name": "PodCrashLoopBackOff",
            "namespace": "payments",
            "cluster": "prod-a",
            "service": "payment-api",
            "summary": "payment-api pod crash loop",
        },
        logs_adapter=recommended_logs,
        k8s_read_adapter=recommended_k8s,
    )

    legacy_k8s_step = legacy_session["steps"][0]
    recommended_k8s_step = recommended_session["steps"][0]

    assert legacy_k8s.calls[0]["selector"] == "app=payment-api"
    assert legacy_k8s_step["status"] == "partial"
    assert legacy_k8s_step["audit"]["resource_match_count"] == 0
    assert legacy_session["diagnosis"]["evidence_chain"][0]["confidence"] == 0.25
    assert recommended_k8s.calls[0]["selector"] == "app.kubernetes.io/name=payment-api"
    assert recommended_k8s_step["status"] == "succeeded"
    assert recommended_k8s_step["audit"]["resource_match_count"] == 1
    assert recommended_session["diagnosis"]["evidence_chain"][0]["confidence"] == 0.8


@pytest.mark.asyncio
async def test_diagnosis_session_records_selector_from_explicit_k8s_argv() -> None:
    k8s = FakeAdapter(
        [
            _envelope(
                "run_k8s_read",
                summary="payment-api pod list returned 1 matching resource",
                source="k8s_gateway",
                ref_id="ev_k8s_payment_pods",
                data={"items": [{"metadata": {"name": "payment-api-7f"}}]},
            )
        ]
    )
    logs = FakeAdapter(
        [
            _envelope(
                "query_logs",
                summary="payment-api logs include upstream timeout",
                source="loki",
                ref_id="ev_loki_payment_timeout",
            )
        ]
    )

    session = await run_diagnosis_session(
        {
            "incident_id": "incident-k8s-explicit-selector",
            "alert_name": "PodCrashLoopBackOff",
            "namespace": "payments",
            "cluster": "prod-a",
            "service": "payment-api",
            "summary": "payment-api pod crash loop",
            "k8s_read_argv": ["kubectl", "get", "pods", "-n", "payments", "-l", "app=payment-api"],
        },
        logs_adapter=logs,
        k8s_read_adapter=k8s,
    )

    assert k8s.calls[0]["selector"] == "app=payment-api"
    assert session["steps"][0]["audit"]["selector"] == "app=payment-api"


@pytest.mark.asyncio
async def test_diagnosis_session_zero_k8s_matches_is_low_confidence_partial_evidence() -> None:
    k8s = FakeAdapter(
        [
            _envelope(
                "run_k8s_read",
                summary="No resources found in payments namespace.",
                source="k8s_gateway",
                ref_id="ev_k8s_payment_empty",
                data={"items": []},
            )
        ]
    )
    logs = FakeAdapter(
        [
            _envelope(
                "query_logs",
                summary="payment-api logs include upstream timeout",
                source="loki",
                ref_id="ev_loki_payment_timeout",
            )
        ]
    )

    session = await run_diagnosis_session(
        {
            "incident_id": "incident-k8s-zero-match",
            "alert_name": "PodCrashLoopBackOff",
            "namespace": "payments",
            "cluster": "prod-a",
            "service": "payment-api",
            "summary": "payment-api pod crash loop",
        },
        logs_adapter=logs,
        k8s_read_adapter=k8s,
    )

    k8s_step = session["steps"][0]
    k8s_evidence = session["diagnosis"]["evidence_chain"][0]

    assert session["status"] == "partial"
    assert k8s_step["status"] == "partial"
    assert k8s_step["summary"] == (
        "K8s selector app.kubernetes.io/name=payment-api returned 0 matching resources; "
        "treating this read as low-confidence evidence."
    )
    assert k8s_step["audit"]["resource_match_count"] == 0
    assert k8s_evidence["payload"]["selector"] == "app.kubernetes.io/name=payment-api"
    assert k8s_evidence["payload"]["resource_match_count"] == 0
    assert k8s_evidence["confidence"] == 0.25


@pytest.mark.asyncio
async def test_diagnosis_session_crashloop_proposes_approval_required_mutation() -> None:
    k8s = FakeAdapter(
        [
            _envelope(
                "run_k8s_read",
                summary="pod/checkout-7d9 is CrashLoopBackOff with exit code 1",
                source="k8s_gateway",
                ref_id="ev_k8s_checkout_pod",
            )
        ]
    )
    logs = FakeAdapter(
        [
            _envelope(
                "query_logs",
                summary="checkout application exits after missing DATABASE_URL",
                source="loki",
                ref_id="ev_loki_checkout_config",
            )
        ]
    )

    session = await run_diagnosis_session(
        {
            "incident_id": "incident-2",
            "alert_name": "PodCrashLoopBackOff",
            "namespace": "checkout",
            "cluster": "prod-a",
            "service": "checkout",
            "summary": "checkout pod crash loop",
        },
        logs_adapter=logs,
        k8s_read_adapter=k8s,
    )

    assert session["status"] == "diagnosed"
    assert [step["tool"] for step in session["steps"]] == ["run_k8s_read", "query_logs"]
    assert session["action_proposals"][0]["approval_required"] is True
    assert session["action_proposals"][0]["execute_automatically"] is False
    assert "workload crash loop" in session["diagnosis"]["root_cause_candidates"][0]["cause"]


@pytest.mark.asyncio
async def test_diagnosis_session_partial_records_topology_missing_reason_and_gateway_placeholder() -> None:
    metrics = FakeAdapter(
        [
            _envelope(
                "query_metrics",
                summary="payment-api 5xx error rate is 4%",
                source="prometheus",
                ref_id="ev_prom_payment_5xx",
            )
        ]
    )
    logs = FakeAdapter(
        [
            _envelope(
                "query_logs",
                summary="payment-api log samples show upstream 503 responses",
                source="loki",
                ref_id="ev_loki_payment_503",
            )
        ]
    )
    topology = FakeAdapter(
        [
            _envelope(
                "get_service_topology",
                status="partial",
                summary="service topology not found",
                source="topology",
                error_code=ErrorCode.SERVICE_NOT_FOUND,
            )
        ]
    )

    session = await run_diagnosis_session(
        {
            "incident_id": "incident-3",
            "alert_name": "PaymentErrorRateHigh",
            "namespace": "payments",
            "cluster": "prod-a",
            "service": "payment-api",
        },
        metrics_adapter=metrics,
        logs_adapter=logs,
        topology_adapter=topology,
        k8s_read_adapter=None,
    )

    assert session["status"] == "partial"
    missing = {step["tool"]: step["missing_reason"] for step in session["steps"] if step["missing_reason"]}
    assert missing["run_k8s_read"] == "Gateway run_k8s_read adapter unavailable"
    assert missing["get_service_topology"] == "service topology not found"
    assert any(item["source_type"] == "topology" for item in session["missing_evidence"])


@pytest.mark.asyncio
async def test_diagnosis_session_partial_evidence_ref_keeps_session_partial() -> None:
    metrics = FakeAdapter(
        [
            _envelope(
                "query_metrics",
                status="partial",
                summary="payment-api 5xx series were truncated at max_series",
                source="prometheus",
                ref_id="ev_prom_payment_5xx_partial",
            )
        ]
    )
    logs = FakeAdapter(
        [
            _envelope(
                "query_logs",
                status="partial",
                summary="payment-api logs were truncated but include upstream timeouts",
                source="loki",
                ref_id="ev_loki_payment_timeout_partial",
            )
        ]
    )
    topology = FakeAdapter(
        [
            _envelope(
                "get_service_topology",
                summary="payment-api depends on billing-api",
                source="topology",
                ref_id="ev_topology_payment",
            )
        ]
    )
    k8s = FakeAdapter(
        [
            _envelope(
                "run_k8s_read",
                summary="deployment/payment-api is available and pods are ready",
                source="k8s_gateway",
                ref_id="ev_k8s_payment_deploy",
            )
        ]
    )

    session = await run_diagnosis_session(
        {
            "incident_id": "incident-6",
            "alert_name": "PaymentErrorRateHigh",
            "namespace": "payments",
            "cluster": "prod-a",
            "service": "payment-api",
            "summary": "payment-api 5xx rate elevated",
        },
        metrics_adapter=metrics,
        logs_adapter=logs,
        topology_adapter=topology,
        k8s_read_adapter=k8s,
    )

    assert session["status"] == "partial"
    assert session["steps"][0]["status"] == "partial"
    assert session["steps"][0]["evidence_ref"] == "ev_prom_payment_5xx_partial"
    assert session["steps"][1]["status"] == "partial"
    assert session["steps"][1]["evidence_ref"] == "ev_loki_payment_timeout_partial"
    assert session["missing_evidence"] == []


@pytest.mark.asyncio
async def test_diagnosis_session_needs_human_when_no_non_memory_evidence() -> None:
    session = await run_diagnosis_session(
        {
            "incident_id": "incident-4",
            "alert_name": "UnknownHighLatency",
            "namespace": "default",
            "cluster": "prod-a",
            "service": "api",
            "memory_hints": [{"source": "incident_memory", "summary": "similar outage was cache saturation"}],
        },
        metrics_adapter=None,
        logs_adapter=None,
        topology_adapter=None,
        k8s_read_adapter=None,
    )

    assert session["status"] == "needs_human"
    assert session["diagnosis"]["evidence_chain"] == []
    assert session["diagnosis"]["optional_memory_hints"][0]["weight"] == "optional"
    assert all(step["missing_reason"] for step in session["steps"])


@pytest.mark.asyncio
async def test_diagnosis_session_backend_unavailable_fails_controlled() -> None:
    metrics = FakeAdapter(
        [
            _envelope(
                "query_metrics",
                status="failed",
                summary="prometheus backend unavailable",
                source="prometheus",
                error_code=ErrorCode.BACKEND_UNAVAILABLE,
            )
        ]
    )

    session = await run_diagnosis_session(
        {
            "incident_id": "incident-5",
            "alert_name": "PaymentErrorRateHigh",
            "namespace": "payments",
            "cluster": "prod-a",
            "service": "payment-api",
        },
        metrics_adapter=metrics,
    )

    assert session["status"] == "failed"
    assert session["steps"][0]["status"] == "failed"
    assert session["steps"][0]["missing_reason"] == "prometheus backend unavailable"
    assert session["diagnosis"]["confidence"]["level"] == "low"


def test_payment_api_high_confidence_diagnosis_is_structured() -> None:
    diagnosis = build_diagnosis(
        incident={"alert_name": "PaymentErrorRateHigh", "namespace": "payments", "cluster": "prod-a"},
        evidence_refs=[
            {
                "source_type": "metrics",
                "source_ref": "promql:payment_5xx_rate",
                "summary": "payment-api 5xx error rate rose from 0.1% to 8%",
                "confidence": 0.9,
            },
            {
                "source_type": "logs",
                "source_ref": "loki:payment-api",
                "summary": "checkout requests show upstream timeout to billing",
                "confidence": 0.85,
            },
            {
                "source_type": "topology",
                "source_ref": "svc:payment-api",
                "summary": "payment-api depends on billing-api",
                "confidence": 0.8,
            },
            {
                "source_type": "k8s_read",
                "source_ref": "deploy/payment-api",
                "summary": "deployment is available and pods are ready",
                "confidence": 0.75,
            },
        ],
        recommended_actions=[
            {"summary": "Query billing-api latency and error metrics", "action_type": "read"},
            {"summary": "Rollback payment-api deployment if regression is confirmed", "action_type": "k8s_write"},
        ],
    )

    parsed = json.loads(to_json(diagnosis))

    assert parsed["confidence"]["level"] == "high"
    assert parsed["trace_refs"] == []
    assert parsed["automation"]["unattended_remediation_allowed"] is False
    assert len(parsed["evidence_chain"]) == 4
    assert parsed["recommended_actions"][0]["approval_required"] is False
    assert parsed["recommended_actions"][1]["approval_required"] is True
    assert parsed["recommended_actions"][1]["execute_automatically"] is False
    assert parsed["markdown"].startswith("# Incident diagnosis: high")


def test_crashloop_high_confidence_uses_k8s_and_logs() -> None:
    diagnosis = build_diagnosis(
        incident={"alert_name": "PodCrashLoopBackOff", "namespace": "checkout", "cluster": "prod-a"},
        evidence_refs=[
            {
                "source_type": "k8s_read",
                "source_ref": "pod/checkout-7d9",
                "summary": "Pod is in CrashLoopBackOff with exit code 1",
                "confidence": 0.8,
            },
            {
                "source_type": "logs",
                "source_ref": "loki:checkout",
                "summary": "application exits after missing DATABASE_URL",
                "confidence": 0.7,
            },
        ],
        recommended_actions=[{"summary": "Patch deployment env after approval", "action_type": "mutation"}],
    )

    assert diagnosis["confidence"]["level"] == "high"
    assert "workload crash loop" in diagnosis["root_cause_candidates"][0]["cause"]
    assert diagnosis["recommended_actions"][0]["approval_required"] is True


def test_single_source_evidence_scores_medium_confidence() -> None:
    diagnosis = build_diagnosis(
        incident={"alert_name": "PodCrashLoopBackOff", "namespace": "checkout", "cluster": "prod-a"},
        evidence_refs=[
            {
                "source_type": "k8s_read",
                "source_ref": "pod/checkout-7d9",
                "summary": "Pod is in CrashLoopBackOff with exit code 1",
                "confidence": 0.65,
            }
        ],
    )

    assert diagnosis["confidence"]["level"] == "medium"
    assert diagnosis["open_questions"]


def test_missing_evidence_degrades_to_low_confidence() -> None:
    diagnosis = build_diagnosis(
        incident={"alert_name": "UnknownHighLatency", "namespace": "default", "cluster": "prod-a"},
        evidence_refs=[],
    )

    assert diagnosis["confidence"] == {"score": 0.2, "level": "low"}
    assert diagnosis["evidence_chain"] == []
    assert diagnosis["root_cause_candidates"][0]["cause"] == "insufficient non-memory evidence"
    assert "metrics/logs/topology/k8s_read evidence" in diagnosis["open_questions"][0]


def test_memory_hint_is_never_unique_evidence() -> None:
    diagnosis = build_diagnosis(
        incident={"alert_name": "PaymentErrorRateHigh", "namespace": "payments", "cluster": "prod-a"},
        evidence_refs=[],
        memory_hints=[{"source": "incident_memory", "summary": "Similar outage was billing timeout"}],
    )

    assert diagnosis["confidence"]["level"] == "low"
    assert diagnosis["evidence_chain"] == []
    assert diagnosis["optional_memory_hints"][0]["weight"] == "optional"
    assert diagnosis["root_cause_candidates"][0]["evidence_refs"] == []
    assert diagnosis["root_cause_candidates"][0]["optional_hints"] == ["Similar outage was billing timeout"]


def test_mutation_recommendations_require_approval_even_when_unspecified() -> None:
    diagnosis = build_diagnosis(
        incident={"alert_name": "PodCrashLoopBackOff", "namespace": "checkout", "cluster": "prod-a"},
        evidence_refs=[
            {
                "source_type": "k8s_read",
                "source_ref": "pod/checkout-7d9",
                "summary": "Pod restart count is increasing",
            }
        ],
        recommended_actions=[{"summary": "kubectl scale deployment checkout-api to 0 then 3"}],
    )

    assert diagnosis["recommended_actions"][0]["approval_required"] is True
    assert diagnosis["recommended_actions"][0]["execute_automatically"] is False
