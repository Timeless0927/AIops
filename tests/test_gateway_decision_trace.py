"""Gateway Decision Trace projection contracts."""

from __future__ import annotations

import json
from pathlib import Path

from apps.aiops_k8s_gateway.decision_trace import project_decision_trace
from apps.aiops_k8s_gateway.diagnosis_delivery import DiagnosisDelivery
from apps.aiops_k8s_gateway.investigation_events import InvestigationEvents
from tests.test_gateway_diagnosis_delivery import Clock, _incident_service, _signal


def test_projection_is_readable_grounded_and_excludes_private_payloads() -> None:
    trace = project_decision_trace(
        {
            "status": "partial",
            "diagnosis": {
                "summary": "错误率升高与 upstream timeout 相关",
                "root_cause_candidates": [
                    {
                        "cause": "upstream timeout",
                        "confidence": 0.72,
                        "evidence_refs": ["evidence:logs:1"],
                        "chain_of_thought": "private reasoning",
                    }
                ],
                "confidence": {"score": 0.72, "level": "medium"},
                "system_prompt": "hidden prompt",
            },
            "hypothesis_state": {
                "goal": "定位 checkout-api 错误率升高原因",
                "candidates": [
                    {
                        "cause": "upstream timeout",
                        "evidence_relations": [
                            {"evidence_ref": "evidence:logs:1", "relation": "supports"},
                            {"evidence_ref": "evidence:metrics:1", "relation": "uncertain"},
                        ],
                        "unknowns": ["upstream saturation 尚未确认"],
                        "next_checks": [],
                        "chain_of_thought": "must not persist",
                    }
                ],
            },
            "completion_validation": {
                "status": "accepted",
                "issues": [],
                "repair_attempts": 0,
                "stopping_reason": "validated",
                "remaining_evidence_steps": 20,
                "raw_payload": {"credential": "secret-token"},
            },
            "tool_activity": [
                {
                    "tool": "query_logs",
                    "purpose": "检查 Loki 日志中的代表性错误",
                    "authorized_scope": {"cluster_id": "prod-a", "namespace": "payments"},
                    "time_range": {"time_range": {"type": "relative", "value": "30m"}},
                    "status": "partial",
                    "duration_ms": 125,
                    "summary": "发现 upstream timeout 样本",
                    "evidence_ref": "evidence:logs:1",
                    "truncation": {"truncated": True, "limit_bytes": 4096},
                    "redaction": {"applied": True, "note": "敏感字段已移除"},
                    "representative_samples": ["authorization: Bearer secret-token"],
                    "raw_payload": {"password": "secret-token"},
                },
                {
                    "tool": "query_metrics",
                    "purpose": "检查 Prometheus 指标",
                    "authorized_scope": {"cluster_id": "prod-a", "namespace": "payments"},
                    "status": "failed",
                    "duration_ms": 80,
                    "summary": "Prometheus unavailable",
                    "missing_reason": "backend timeout",
                    "evidence_ref": None,
                    "credential": "secret-token",
                },
            ],
        }
    )

    assert trace["goal"] == "定位 checkout-api 错误率升高原因"
    assert trace["completion"] == {
        "status": "accepted",
        "issues": [],
        "repair_attempts": 0,
        "stopping_reason": "validated",
        "remaining_evidence_steps": 20,
    }
    assert trace["candidates"] == [
        {
            "cause": "upstream timeout",
            "confidence": 0.72,
            "evidence_relations": [
                {"evidence_ref": "evidence:logs:1", "relation": "supports"},
                {"evidence_ref": "evidence:metrics:1", "relation": "uncertain"},
            ],
            "unknowns": ["upstream saturation 尚未确认"],
            "next_checks": [],
        }
    ]
    assert trace["tool_activity"][0]["candidate_impacts"] == [
        {"cause": "upstream timeout", "relation": "supports"}
    ]
    assert trace["tool_activity"][0]["continuation_reason"] == "继续检查剩余候选原因或必需来源"
    assert trace["tool_activity"][1]["stopping_reason"] == "validated"
    assert trace["tool_activity"][1]["missing_reason"] == "backend timeout"
    assert trace["tool_activity"][1]["status_reason"] == "backend timeout"
    assert trace["tool_activity"][0]["truncation"]["truncated"] is True
    assert trace["tool_activity"][0]["truncation"]["reason"] == "已按 4096 字节安全上限截断"
    assert trace["tool_activity"][0]["redaction"]["applied"] is True
    assert trace["tool_activity"][0]["redaction"]["note"] == "敏感字段已移除"
    serialized = json.dumps(trace, ensure_ascii=False)
    for forbidden in ("private reasoning", "hidden prompt", "secret-token", "raw_payload", "chain_of_thought"):
        assert forbidden not in serialized


def test_projection_explains_skipped_activity() -> None:
    trace = project_decision_trace({
        "tool_activity": [{
            "tool": "query_logs",
            "status": "skipped",
            "summary": "日志 Evidence budget 已耗尽",
        }],
    })

    assert trace["tool_activity"][0]["status_reason"] == "日志 Evidence budget 已耗尽"


def test_writeback_projects_replayable_decision_trace_without_private_fields(tmp_path: Path) -> None:
    clock = Clock()
    db_path = tmp_path / "gateway.db"
    incidents = _incident_service(db_path, clock)
    incident_id = str(incidents.ingest(_signal())["incident"]["id"])  # type: ignore[index]
    sent: list[dict[str, object]] = []
    delivery = DiagnosisDelivery(
        db_path,
        send=lambda payload: (
            sent.append(payload) or 202,
            {"status": "accepted", "request_id": payload["request_id"]},
        ),
        clock=clock,
    )
    delivery.reconcile_due()
    payload = {
        "request_id": sent[0]["request_id"],
        "incident_id": incident_id,
        "investigation_id": sent[0]["investigation_id"],
        "provider_revision": "model-provider:revision-trace-1",
        "status": "partial",
        "diagnosis": {
            "summary": "日志支持 upstream timeout，指标来源不可用",
            "root_cause_candidates": [
                {"cause": "upstream timeout", "confidence": 0.7, "evidence_refs": ["loki:timeout"]}
            ],
            "confidence": {"score": 0.7, "level": "medium"},
            "system_prompt": "hidden prompt",
        },
        "hypothesis_state": {
            "goal": "定位 checkout-api 错误率升高原因",
            "candidates": [{
                "cause": "upstream timeout",
                "evidence_relations": [{"evidence_ref": "loki:timeout", "relation": "supports"}],
                "unknowns": ["Prometheus unavailable"],
                "next_checks": [],
                "chain_of_thought": "private reasoning",
            }],
        },
        "completion_validation": {
            "status": "accepted", "issues": [], "repair_attempts": 0,
            "stopping_reason": "required_source_missing", "remaining_evidence_steps": 22,
        },
        "tool_activity": [
            {
                "tool": "query_logs", "purpose": "检查 Loki 日志",
                "authorized_scope": {"cluster_id": "cluster-prod", "namespace": "payments"},
                "status": "succeeded", "duration_ms": 120, "summary": "发现 upstream timeout",
                "evidence_ref": "loki:timeout",
                "truncation": {"truncated": False, "limit_bytes": 4096},
                "redaction": {"applied": True, "note": "敏感字段已移除"},
                "raw_payload": {"credential": "secret-token"},
            },
            {
                "tool": "query_metrics", "purpose": "检查 Prometheus 指标",
                "authorized_scope": {"cluster_id": "cluster-prod", "namespace": "payments"},
                "status": "failed", "duration_ms": 80, "summary": "Prometheus unavailable",
                "missing_reason": "backend timeout", "evidence_ref": None,
            },
        ],
        "steps": [{
            "tool": "query_logs", "status": "succeeded", "source_type": "logs",
            "evidence_ref": {"ref_id": "loki:timeout"}, "summary": "发现 upstream timeout",
        }],
        "missing_evidence": [{"source_type": "metrics", "reason": "Prometheus unavailable"}],
    }

    assert delivery.accept_writeback(payload) == {"ok": True, "duplicate": False}
    assert delivery.accept_writeback(payload) == {"ok": True, "duplicate": True}
    events = InvestigationEvents(db_path).list(str(sent[0]["investigation_id"]))["events"]
    diagnosis_event = next(event for event in events if event["type"] == "diagnosis.output")
    tool_events = [event for event in events if event["type"] == "tool.activity"]
    assert diagnosis_event["payload"]["decision_trace"]["completion"]["stopping_reason"] == "required_source_missing"
    assert diagnosis_event["payload"]["decision_trace"]["candidates"][0]["confidence"] == 0.7
    assert tool_events[0]["payload"]["candidate_impacts"] == [
        {"cause": "upstream timeout", "relation": "supports"}
    ]
    assert tool_events[1]["payload"]["missing_reason"] == "backend timeout"
    assert tool_events[1]["payload"]["stopping_reason"] == "required_source_missing"
    serialized = str(events)
    for forbidden in ("hidden prompt", "private reasoning", "secret-token", "raw_payload", "chain_of_thought"):
        assert forbidden not in serialized
        assert forbidden.encode() not in db_path.read_bytes()
