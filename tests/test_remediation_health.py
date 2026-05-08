"""Focused tests for remediation health checks."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from toolsets import incident_store, message_delivery, remediation_health


def _scale_action() -> dict[str, Any]:
    return {
        "action_type": "scale_deployment",
        "action_signature": "scale_deployment:prod-a:default:deployment/nginx:replicas=3",
        "cluster": "prod-a",
        "namespace": "default",
        "resource_kind": "deployment",
        "resource_name": "nginx",
        "parameters": {"replicas": 3},
    }


def _deployment(*, available: int, updated: int, unavailable: int = 0) -> dict[str, Any]:
    return {
        "metadata": {"name": "nginx", "generation": 2},
        "spec": {
            "replicas": 3,
            "selector": {"matchLabels": {"app": "nginx"}},
        },
        "status": {
            "availableReplicas": available,
            "updatedReplicas": updated,
            "unavailableReplicas": unavailable,
            "observedGeneration": 2,
        },
    }


def _pods(ready: int) -> dict[str, Any]:
    items = []
    for index in range(ready):
        items.append(
            {
                "metadata": {"name": f"nginx-{index}"},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
            }
        )
    return {"items": items}


def _kubectl_result(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "exit_code": 0,
        "stdout": json.dumps(payload),
        "stderr": "",
    }


def test_health_check_returns_healthy_for_ready_rollout(monkeypatch) -> None:
    async def fake_run(command: str, context: str | None = None) -> dict[str, Any]:
        assert context == "prod-a"
        if "get deployment/nginx" in command:
            return _kubectl_result(_deployment(available=3, updated=3))
        if "get pods" in command:
            return _kubectl_result(_pods(ready=3))
        if "get events" in command:
            return _kubectl_result({"items": []})
        raise AssertionError(command)

    monkeypatch.setattr(remediation_health, "_run_kubectl", fake_run)

    result = asyncio.run(remediation_health.check_action_health(_scale_action(), timeout_seconds=0))

    assert result["ok"] is True
    assert result["status"] == "healthy"
    assert {check["name"] for check in result["checks"]} == {
        "deployment_available",
        "deployment_generation_observed",
        "deployment_updated",
        "pods_ready",
        "no_new_warning_events",
    }


def test_health_check_fails_when_replicas_unavailable(monkeypatch) -> None:
    async def fake_run(command: str, context: str | None = None) -> dict[str, Any]:
        if "get deployment/nginx" in command:
            return _kubectl_result(_deployment(available=1, updated=3, unavailable=2))
        if "get pods" in command:
            return _kubectl_result(_pods(ready=1))
        if "get events" in command:
            return _kubectl_result({"items": []})
        raise AssertionError(command)

    monkeypatch.setattr(remediation_health, "_run_kubectl", fake_run)

    result = asyncio.run(remediation_health.check_action_health(_scale_action(), timeout_seconds=0))

    assert result["ok"] is False
    assert result["status"] == "rollback_required"
    assert result["reason_code"] == "deployment_unavailable"
    assert result["rollback_required"] is True


def test_failed_health_records_rollback_required_status_timeline_and_notification(tmp_path: Path) -> None:
    _reset_stores(tmp_path)

    incident_id = asyncio.run(
        incident_store.create_incident(
            "DeploymentUnavailable",
            "default",
            "prod-a",
            "nginx unavailable",
            platform="feishu",
            chat_id="oc_ops",
            thread_id="omt_thread",
        )
    )
    asyncio.run(incident_store.update_status(incident_id, "triaging"))
    asyncio.run(incident_store.update_status(incident_id, "investigating"))
    asyncio.run(incident_store.update_status(incident_id, "pending_approval"))

    health_result = {
        "ok": False,
        "status": "rollback_required",
        "reason_code": "deployment_unavailable",
        "summary": "1/3 replicas available",
        "checks": [],
        "rollback_required": True,
    }
    record = asyncio.run(
        remediation_health.record_rollback_required(
            incident_id=incident_id,
            action=_scale_action(),
            health_result=health_result,
            approval_id="approval-1",
        )
    )

    incident = asyncio.run(incident_store.get_incident(incident_id))
    timeline = asyncio.run(incident_store.get_timeline(incident_id))
    pending = asyncio.run(message_delivery.list_pending())

    assert record["ok"] is True
    assert incident["status"] == "rollback_required"
    assert timeline[-1]["event_type"] == "rollback_required"
    assert timeline[-1]["metadata"]["reason_code"] == "deployment_unavailable"
    assert timeline[-1]["metadata"]["previous_status"] == "pending_approval"
    assert pending[0]["target_type"] == "rollback_required"
    assert record["delivery"]["delivery_id"] == pending[0]["id"]


def _reset_stores(tmp_path: Path) -> None:
    incident_store._STORE.close()
    incident_store._STORE = incident_store.IncidentStore(tmp_path / "incidents.db")
    message_delivery._DB.close()
    message_delivery._DB = message_delivery.MessageDeliveryDB(tmp_path / "message_deliveries.db")
