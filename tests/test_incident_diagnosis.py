"""Tests for AIO-51 incident diagnosis runtime skeleton."""

from __future__ import annotations

import json

from toolsets.incident_diagnosis import build_diagnosis, to_json


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
