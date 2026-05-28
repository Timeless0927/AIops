"""Focused tests for deterministic remediation rollback."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from toolsets import audit_log, incident_store, operation_lock, remediation_rollback


def _scale_action() -> dict[str, Any]:
    return {
        "action_schema_version": "remediation.action.v1",
        "action_type": "scale_deployment",
        "action_signature": "scale_deployment:prod-a:default:deployment/nginx:replicas=3",
        "cluster": "prod-a",
        "namespace": "default",
        "resource_kind": "deployment",
        "resource_name": "nginx",
        "parameters": {"replicas": 3},
        "risk": {"risk_level": "low", "operation_type": "k8s_write"},
        "before": {"replicas": 2},
        "after": {"replicas": 3},
    }


def test_build_scale_deployment_rollback_action() -> None:
    rollback = remediation_rollback.build_rollback_action(_scale_action())

    assert rollback["ok"] is True
    assert rollback["command_preview"] == "kubectl scale deployment/nginx --replicas=2 -n default"
    assert rollback["dry_run_command_preview"] == (
        "kubectl scale deployment/nginx --replicas=2 -n default --dry-run=server"
    )
    assert rollback["context"] == "prod-a"
    assert rollback["kube_context"] is None
    assert rollback["resource_key"] == "k8s:prod-a:default:deployment/nginx"
    assert rollback["rollback_action"]["parameters"] == {"replicas": 2}
    assert rollback["rollback_action"]["action_signature"] == (
        "scale_deployment:prod-a:default:deployment/nginx:replicas=2"
    )


def test_build_rollback_action_uses_explicit_kube_context_mapping(monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_KUBE_CONTEXT_MAP", '{"206K8S":"prod-admin"}')
    action = _scale_action()
    action["cluster"] = "206K8S"
    action["action_signature"] = "scale_deployment:206K8S:default:deployment/nginx:replicas=3"

    rollback = remediation_rollback.build_rollback_action(action)

    assert rollback["ok"] is True
    assert rollback["context"] == "206K8S"
    assert rollback["kube_context"] == "prod-admin"
    assert rollback["resource_key"] == "k8s:206K8S:default:deployment/nginx"


def test_unsupported_rollback_action_is_refused() -> None:
    action = {
        "action_schema_version": "remediation.action.v1",
        "action_type": "restart_deployment",
        "action_signature": "restart_deployment:prod-a:default:deployment/nginx",
        "cluster": "prod-a",
        "namespace": "default",
        "resource_kind": "deployment",
        "resource_name": "nginx",
        "parameters": {"strategy": "rollout_restart"},
        "risk": {"risk_level": "low", "operation_type": "k8s_write"},
    }

    rollback = remediation_rollback.build_rollback_action(action)

    assert rollback["ok"] is False
    assert rollback["reason_code"] == "unsupported_rollback_action"
    assert rollback["rollback_action"] is None


def test_missing_cluster_is_refused_fail_closed() -> None:
    action = _scale_action()
    action["cluster"] = ""

    rollback = remediation_rollback.build_rollback_action(action)

    assert rollback["ok"] is False
    assert rollback["reason_code"] == "invalid_cluster"
    assert rollback["rollback_action"] is None


def test_execute_rollback_writes_incident_timeline(tmp_path: Path, monkeypatch) -> None:
    _reset_stores(tmp_path)
    incident_id = asyncio.run(
        incident_store.create_incident("DeploymentUnavailable", "default", "prod-a", "nginx unavailable")
    )
    asyncio.run(incident_store.update_status(incident_id, "triaging"))
    asyncio.run(incident_store.update_status(incident_id, "investigating"))
    asyncio.run(incident_store.update_status(incident_id, "pending_approval"))
    asyncio.run(incident_store.update_status(incident_id, "executing"))
    asyncio.run(incident_store.update_status(incident_id, "verifying"))
    asyncio.run(
        incident_store.mark_rollback_required(
            incident_id,
            reason_code="deployment_unavailable",
            summary="1/3 replicas available",
        )
    )

    calls: list[str] = []

    async def fake_dry_run(action: dict[str, Any], *, max_replicas: int = 20) -> dict[str, Any]:
        calls.append("dry-run")
        assert action["cluster"] == "prod-a"
        assert action["parameters"] == {"replicas": 2}
        return {
            "ok": True,
            "mode": "server",
            "command_preview": "kubectl scale deployment/nginx --replicas=2 -n default --dry-run=server",
            "summary": "server dry-run accepted",
        }

    async def fake_execute(
        command: str,
        context: str | None = None,
        *,
        kube_context: str | None = None,
    ) -> dict[str, Any]:
        calls.append("execute")
        assert command == "kubectl scale deployment/nginx --replicas=2 -n default"
        assert context == "prod-a"
        assert kube_context is None
        return {
            "ok": True,
            "command": command,
            "context": context,
            "kube_context": kube_context,
            "exit_code": 0,
            "stdout": "scaled",
            "stderr": "",
        }

    monkeypatch.setattr(remediation_rollback, "dry_run_action", fake_dry_run)
    monkeypatch.setattr(remediation_rollback, "execute_approved", fake_execute)

    result = asyncio.run(
        remediation_rollback.execute_rollback(
            _scale_action(),
            incident_id=incident_id,
            approver_id="ops-1",
            approval_id="approval-1",
        )
    )
    timeline = asyncio.run(incident_store.get_timeline(incident_id))
    locked = asyncio.run(operation_lock.is_locked(result["resource_key"]))

    assert result["ok"] is True
    assert result["dry_run"]["ok"] is True
    assert result["started_audit_id"] > 0
    assert result["audit_id"] > 0
    assert calls == ["dry-run", "execute"]
    assert locked is False
    assert timeline[-2]["event_type"] == "rollback_started"
    assert timeline[-1]["event_type"] == "rollback_executed"
    assert timeline[-1]["metadata"]["rollback"]["command_preview"] == (
        "kubectl scale deployment/nginx --replicas=2 -n default"
    )


def test_execute_rollback_refuses_when_dry_run_fails(tmp_path: Path, monkeypatch) -> None:
    _reset_stores(tmp_path)
    incident_id = asyncio.run(
        incident_store.create_incident("DeploymentUnavailable", "default", "prod-a", "nginx unavailable")
    )
    asyncio.run(incident_store.update_status(incident_id, "triaging"))
    asyncio.run(incident_store.update_status(incident_id, "investigating"))
    asyncio.run(incident_store.update_status(incident_id, "pending_approval"))
    asyncio.run(incident_store.mark_rollback_required(
        incident_id,
        reason_code="deployment_unavailable",
        summary="1/3 replicas available",
    ))

    async def fake_dry_run(action: dict[str, Any], *, max_replicas: int = 20) -> dict[str, Any]:
        return {
            "ok": False,
            "reason_code": "dry_run_failed",
            "command_preview": "kubectl scale deployment/nginx --replicas=2 -n default --dry-run=server",
            "summary": "server dry-run failed",
        }

    async def fake_execute(
        command: str,
        context: str | None = None,
        *,
        kube_context: str | None = None,
    ) -> dict[str, Any]:
        raise AssertionError("rollback execution must not run after dry-run failure")

    monkeypatch.setattr(remediation_rollback, "dry_run_action", fake_dry_run)
    monkeypatch.setattr(remediation_rollback, "execute_approved", fake_execute)

    result = asyncio.run(
        remediation_rollback.execute_rollback(
            _scale_action(),
            incident_id=incident_id,
            approver_id="ops-1",
            approval_id="approval-1",
        )
    )
    timeline = asyncio.run(incident_store.get_timeline(incident_id))

    assert result["ok"] is False
    assert result["status"] == "dry_run_failed"
    assert result["audit_id"] > 0
    assert timeline[-1]["event_type"] == "rollback_failed"
    assert timeline[-1]["metadata"]["rollback"]["reason_code"] == "dry_run_failed"


def test_execute_rollback_refuses_when_operation_lock_busy(tmp_path: Path, monkeypatch) -> None:
    _reset_stores(tmp_path)
    rollback = remediation_rollback.build_rollback_action(_scale_action())
    assert rollback["ok"] is True
    asyncio.run(operation_lock.acquire_lock(rollback["resource_key"], "other-session", 300))

    async def fake_dry_run(action: dict[str, Any], *, max_replicas: int = 20) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "server",
            "command_preview": "kubectl scale deployment/nginx --replicas=2 -n default --dry-run=server",
            "summary": "server dry-run accepted",
        }

    async def fake_execute(
        command: str,
        context: str | None = None,
        *,
        kube_context: str | None = None,
    ) -> dict[str, Any]:
        raise AssertionError("rollback execution must not run while operation lock is busy")

    monkeypatch.setattr(remediation_rollback, "dry_run_action", fake_dry_run)
    monkeypatch.setattr(remediation_rollback, "execute_approved", fake_execute)

    result = asyncio.run(
        remediation_rollback.execute_rollback(
            _scale_action(),
            approver_id="ops-1",
            approval_id="approval-1",
        )
    )

    assert result["ok"] is False
    assert result["status"] == "lock_busy"
    assert result["reason_code"] == "operation_locked"
    assert result["audit_id"] > 0


def _reset_stores(tmp_path: Path) -> None:
    incident_store._STORE.close()
    incident_store._STORE = incident_store.IncidentStore(tmp_path / "incidents.db")
    audit_log._DB.close()
    audit_log._DB = audit_log.AuditLogDB(tmp_path / "audit_log.db")
    operation_lock._DB.close()
    operation_lock._DB = operation_lock.OperationLockDB(tmp_path / "operation_locks.db")
