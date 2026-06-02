"""Focused tests for the remediation execution adapter."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from toolsets import remediation_execution


@pytest.fixture(autouse=True)
def _clear_kube_context_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIOPS_KUBE_CONTEXT_MAP", raising=False)
    monkeypatch.delenv("AIOPS_CLUSTER_CONTEXT_MAP", raising=False)
    monkeypatch.delenv("HERMES_CONFIG", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)


def _scale_action() -> dict[str, object]:
    return {
        "action_schema_version": "remediation.action.v1",
        "action_signature": "scale_deployment:prod-a:default:deployment/nginx:replicas=3",
        "action_type": "scale_deployment",
        "cluster": "prod-a",
        "namespace": "default",
        "resource_kind": "deployment",
        "resource_name": "nginx",
        "parameters": {"replicas": 3},
        "source": {
            "incident_id": "inc-1",
            "alertname": "KubeDeploymentReplicasMismatch",
            "analysis_action": "scale deployment/nginx to 3 replicas",
        },
        "risk": {"risk_level": "low", "operation_type": "k8s_write"},
    }


def _restart_action() -> dict[str, object]:
    return {
        "action_schema_version": "remediation.action.v1",
        "action_signature": "restart_deployment:prod-a:prod:deployment/api",
        "action_type": "restart_deployment",
        "cluster": "prod-a",
        "namespace": "prod",
        "resource_kind": "deployment",
        "resource_name": "api",
        "parameters": {"strategy": "rollout_restart"},
        "source": {"incident_id": "inc-1"},
        "risk": {"risk_level": "low", "operation_type": "k8s_write"},
    }


def test_dry_run_builds_server_side_scale_command() -> None:
    execute = AsyncMock(return_value={
        "ok": True,
        "stdout": "deployment.apps/nginx scaled (server dry run)",
        "stderr": "",
        "exit_code": 0,
    })

    with patch("toolsets.remediation_execution.k8s_write.execute_approved", new=execute):
        result = asyncio.run(remediation_execution.dry_run_action(_scale_action()))

    assert result["ok"] is True
    assert result["mode"] == "server"
    assert result["command_preview"] == (
        "kubectl scale deployment/nginx --replicas=3 -n default --dry-run=server"
    )
    execute.assert_awaited_once_with(
        "kubectl scale deployment/nginx --replicas=3 -n default --dry-run=server",
        None,
    )


def test_dry_run_builds_server_side_restart_patch_command() -> None:
    execute = AsyncMock(return_value={
        "ok": True,
        "stdout": "deployment.apps/api patched (no change)",
        "stderr": "",
        "exit_code": 0,
    })

    with patch("toolsets.remediation_execution.k8s_write.execute_approved", new=execute):
        result = asyncio.run(remediation_execution.dry_run_action(_restart_action()))

    assert result["ok"] is True
    assert result["mode"] == "server"
    assert result["command_preview"] == (
        "kubectl patch deployment/api -n prod --type=strategic -p '{}' --dry-run=server"
    )
    assert "rollout restart" not in result["command_preview"]
    execute.assert_awaited_once_with(
        "kubectl patch deployment/api -n prod --type=strategic -p '{}' --dry-run=server",
        None,
    )


def test_safe_execute_short_circuits_on_dry_run_failure() -> None:
    execute = AsyncMock(return_value={
        "ok": False,
        "stdout": "",
        "stderr": "deployment not found",
        "exit_code": 1,
    })
    acquire = AsyncMock(return_value=True)
    record_audit = AsyncMock(return_value=42)
    add_event = AsyncMock(return_value=7)

    with patch("toolsets.remediation_execution.k8s_write.execute_approved", new=execute), patch(
        "toolsets.remediation_execution.operation_lock.acquire_lock",
        new=acquire,
    ), patch("toolsets.remediation_execution.audit_log.record_audit", new=record_audit), patch(
        "toolsets.remediation_execution.incident_store.add_event",
        new=add_event,
    ):
        result = asyncio.run(remediation_execution.safe_execute_action(_scale_action(), approval_id="ap-1"))

    assert result["ok"] is False
    assert result["status"] == "dry_run_failed"
    assert result["reason_code"] == "dry_run_failed"
    assert execute.await_count == 1
    acquire.assert_not_awaited()
    record_audit.assert_awaited_once()
    add_event.assert_awaited_once()


def test_safe_execute_rejects_unallowlisted_action() -> None:
    action = _scale_action()
    action["action_signature"] = "delete_pod:prod-a:default:pod/nginx"
    action["action_type"] = "delete_pod"
    action["resource_kind"] = "pod"
    action["resource_name"] = "nginx"
    action["parameters"] = {"name": "nginx"}

    execute = AsyncMock()
    with patch("toolsets.remediation_execution.k8s_write.execute_approved", new=execute):
        result = asyncio.run(remediation_execution.safe_execute_action(action))

    assert result["ok"] is False
    assert result["status"] == "rejected"
    assert result["reason_code"] == "unsupported_action"
    execute.assert_not_awaited()


def test_safe_execute_honors_operation_lock() -> None:
    execute = AsyncMock(return_value={
        "ok": True,
        "stdout": "server dry run accepted",
        "stderr": "",
        "exit_code": 0,
    })
    acquire = AsyncMock(return_value=False)
    release = AsyncMock()
    record_audit = AsyncMock(return_value=43)
    add_event = AsyncMock(return_value=8)

    with patch("toolsets.remediation_execution.k8s_write.execute_approved", new=execute), patch(
        "toolsets.remediation_execution.operation_lock.acquire_lock",
        new=acquire,
    ), patch("toolsets.remediation_execution.operation_lock.release_lock", new=release), patch(
        "toolsets.remediation_execution.audit_log.record_audit",
        new=record_audit,
    ), patch("toolsets.remediation_execution.incident_store.add_event", new=add_event):
        result = asyncio.run(remediation_execution.safe_execute_action(_scale_action(), session_id="s-1"))

    assert result["ok"] is False
    assert result["status"] == "lock_busy"
    assert result["reason_code"] == "operation_locked"
    assert execute.await_count == 1
    acquire.assert_awaited_once_with("k8s:prod-a:default:deployment/nginx", "s-1", 300)
    release.assert_not_awaited()


def test_safe_execute_records_audit_timeline_and_status() -> None:
    execute = AsyncMock(side_effect=[
        {
            "ok": True,
            "stdout": "server dry run accepted",
            "stderr": "",
            "exit_code": 0,
        },
        {
            "ok": True,
            "stdout": "deployment.apps/nginx scaled",
            "stderr": "",
            "exit_code": 0,
            "result": {"line_count": 1},
        },
    ])
    acquire = AsyncMock(return_value=True)
    release = AsyncMock(return_value=True)
    record_audit = AsyncMock(return_value=99)
    add_event = AsyncMock(return_value=77)

    with patch("toolsets.remediation_execution.k8s_write.execute_approved", new=execute), patch(
        "toolsets.remediation_execution.operation_lock.acquire_lock",
        new=acquire,
    ), patch("toolsets.remediation_execution.operation_lock.release_lock", new=release), patch(
        "toolsets.remediation_execution.audit_log.record_audit",
        new=record_audit,
    ), patch("toolsets.remediation_execution.incident_store.add_event", new=add_event):
        result = asyncio.run(remediation_execution.safe_execute_action(
            _scale_action(),
            approval_id="ap-1",
            requested_by="worker-b",
            approval_by="alice",
            session_id="s-1",
        ))

    assert result["ok"] is True
    assert result["status"] == "succeeded"
    assert result["audit_id"] == 99
    assert result["timeline_event_id"] == 77
    assert result["execution"]["command_preview"] == "kubectl scale deployment/nginx --replicas=3 -n default"
    release.assert_awaited_once_with("k8s:prod-a:default:deployment/nginx", "s-1")

    audit_kwargs = record_audit.await_args.kwargs
    assert audit_kwargs["who"] == "worker-b"
    assert audit_kwargs["tool_name"] == "remediation_execution"
    assert audit_kwargs["tool_level"] == "k8s_write"
    assert audit_kwargs["approval_by"] == "alice"
    assert json.loads(audit_kwargs["dry_run"])["ok"] is True
    assert json.loads(audit_kwargs["result"])["status"] == "succeeded"

    add_event.assert_awaited_once_with(
        "inc-1",
        "remediate_executed",
        "remediation_execution",
        "scale_deployment:prod-a:default:deployment/nginx:replicas=3",
        "succeeded",
        {
            "approval_id": "ap-1",
            "resource_key": "k8s:prod-a:default:deployment/nginx",
            "command_preview": "kubectl scale deployment/nginx --replicas=3 -n default",
            "exit_code": 0,
            "audit_id": 99,
        },
    )


def test_execute_action_uses_k8s_write_approved_primitive() -> None:
    execute = AsyncMock(return_value={
        "ok": True,
        "stdout": "deployment.apps/api restarted",
        "stderr": "",
        "exit_code": 0,
        "result": {"line_count": 1},
    })

    with patch("toolsets.remediation_execution.k8s_write.execute_approved", new=execute):
        result = asyncio.run(remediation_execution.execute_action(_restart_action()))

    assert result["ok"] is True
    assert result["command_preview"] == "kubectl rollout restart deployment/api -n prod"
    execute.assert_awaited_once_with(
        "kubectl rollout restart deployment/api -n prod",
        None,
    )


def test_execute_action_uses_explicit_kube_context_when_configured() -> None:
    execute = AsyncMock(return_value={
        "ok": True,
        "stdout": "deployment.apps/api restarted",
        "stderr": "",
        "exit_code": 0,
        "result": {"line_count": 1},
    })
    action = _restart_action()
    action["kube_context"] = "prod-context"

    with patch("toolsets.remediation_execution.k8s_write.execute_approved", new=execute):
        result = asyncio.run(remediation_execution.execute_action(action))

    assert result["ok"] is True
    assert result["command_preview"] == "kubectl rollout restart deployment/api -n prod"
    assert result["kube_context"] == "prod-context"
    execute.assert_awaited_once_with(
        "kubectl rollout restart deployment/api -n prod",
        "prod-context",
    )


def test_dry_run_rejects_invalid_kube_context_before_kubectl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIOPS_KUBE_CONTEXT_MAP", '{"prod-a": "bad context"}')
    execute = AsyncMock()

    with patch("toolsets.remediation_execution.k8s_write.execute_approved", new=execute):
        result = asyncio.run(remediation_execution.dry_run_action(_restart_action()))

    assert result["ok"] is False
    assert result["reason_code"] == "invalid_kube_context"
    assert result["command_preview"] == (
        "kubectl patch deployment/api -n prod --type=strategic -p '{}' --dry-run=server"
    )
    execute.assert_not_awaited()


def test_adapter_dry_run_stage_only_uses_server_side_dry_run() -> None:
    execute = AsyncMock(
        return_value={
            "ok": True,
            "stdout": "server dry run accepted",
            "stderr": "",
            "exit_code": 0,
        }
    )
    composite = AsyncMock(side_effect=AssertionError("safe_execute_action must not run"))
    adapter = remediation_execution.create_approval_execution_adapter()

    with patch("toolsets.remediation_execution.k8s_write.execute_approved", new=execute), patch(
        "toolsets.remediation_execution.safe_execute_action",
        new=composite,
    ):
        result = asyncio.run(adapter.dry_run_action(_scale_action(), {"approval_id": "ap-1"}, {"id": "ex-1"}))

    assert result["ok"] is True
    assert result["command_preview"] == "kubectl scale deployment/nginx --replicas=3 -n default --dry-run=server"
    execute.assert_awaited_once_with(
        "kubectl scale deployment/nginx --replicas=3 -n default --dry-run=server",
        None,
    )
    composite.assert_not_awaited()


def test_adapter_execute_stage_only_uses_real_write_action() -> None:
    execute = AsyncMock(
        return_value={
            "ok": True,
            "stdout": "deployment.apps/nginx scaled",
            "stderr": "",
            "exit_code": 0,
            "result": {"line_count": 1},
        }
    )
    composite = AsyncMock(side_effect=AssertionError("safe_execute_action must not run"))
    adapter = remediation_execution.create_approval_execution_adapter()

    with patch("toolsets.remediation_execution.k8s_write.execute_approved", new=execute), patch(
        "toolsets.remediation_execution.safe_execute_action",
        new=composite,
    ):
        result = asyncio.run(adapter.execute_action(_scale_action(), {"approval_id": "ap-1"}, {"id": "ex-1"}))

    assert result["ok"] is True
    assert result["command_preview"] == "kubectl scale deployment/nginx --replicas=3 -n default"
    assert "--dry-run=server" not in result["command_preview"]
    execute.assert_awaited_once_with(
        "kubectl scale deployment/nginx --replicas=3 -n default",
        None,
    )
    composite.assert_not_awaited()


def test_adapter_health_healthy_allows_success() -> None:
    health = AsyncMock(
        return_value={
            "ok": True,
            "status": "healthy",
            "reason_code": None,
            "summary": "deployment rollout and replicas healthy",
            "checks": [],
            "rollback_required": False,
        }
    )
    adapter = remediation_execution.create_approval_execution_adapter(
        health_timeout_seconds=0,
        health_interval_seconds=0,
    )

    with patch(
        "toolsets.remediation_execution.remediation_health.check_and_record_action_health",
        new=health,
    ):
        result = asyncio.run(
            adapter.check_health(
                _scale_action(),
                {"approval_id": "ap-1", "incident_id": "inc-1"},
                {"id": "ex-1"},
                {"ok": True},
            )
        )

    assert result["ok"] is True
    assert result["status"] == "healthy"
    assert result["stage"] == "health"
    assert result["source"] == "remediation_execution_adapter"
    health.assert_awaited_once()
    assert health.await_args.kwargs["incident_id"] == "inc-1"
    assert health.await_args.kwargs["approval_id"] == "ap-1"
    assert health.await_args.kwargs["timeout_seconds"] == 0
    assert health.await_args.kwargs["interval_seconds"] == 0
    assert health.await_args.kwargs["notify"] is True


def test_adapter_health_missing_incident_fails_closed_without_health_call() -> None:
    health = AsyncMock()
    adapter = remediation_execution.create_approval_execution_adapter(
        health_timeout_seconds=0,
        health_interval_seconds=0,
    )

    with patch(
        "toolsets.remediation_execution.remediation_health.check_and_record_action_health",
        new=health,
    ):
        action = _scale_action()
        action["source"] = {}
        result = asyncio.run(
            adapter.check_health(
                action,
                {"approval_id": "ap-1"},
                {"id": "ex-1"},
                {"ok": True},
            )
        )

    assert result["ok"] is False
    assert result["status"] == "needs_manual_verification"
    assert result["reason_code"] == "health_check_unavailable"
    assert result["health_unavailable_reason"] == "incident_id_missing"
    assert result["needs_manual_verification"] is True
    health.assert_not_awaited()


@pytest.mark.asyncio
async def test_factory_adapter_runs_discrete_stages_into_coordinator(tmp_path, **_kwargs) -> None:
    from toolsets import approval_async, approval_execution

    old_db = approval_async._DB
    approval_async._DB = approval_async.ApprovalDB(tmp_path / "approvals.db")
    try:
        action = _scale_action()
        approval_context = {
            "action_signature": action["action_signature"],
            "executable": True,
            "remediation_action": action,
        }
        approval_id = await approval_async.request_approval(
            "k8s_write",
            "scale deployment/nginx",
            approval_context,
            "default",
            "alert_webhook",
            "low",
            incident_id="inc-1",
        )
        resolved = await approval_async.resolve_approval(approval_id, "approved", "alice")
        assert resolved["ok"] is True

        execute = AsyncMock(side_effect=[
            {
                "ok": True,
                "stdout": "server dry run accepted",
                "stderr": "",
                "exit_code": 0,
            },
            {
                "ok": True,
                "stdout": "deployment.apps/nginx scaled",
                "stderr": "",
                "exit_code": 0,
                "result": {"line_count": 1},
            },
        ])
        acquire = AsyncMock(return_value=True)
        release = AsyncMock(return_value=True)
        record_audit = AsyncMock(return_value=101)
        add_event = AsyncMock(return_value=202)
        health = AsyncMock(
            return_value={
                "ok": True,
                "status": "healthy",
                "reason_code": None,
                "summary": "deployment rollout and replicas healthy",
                "checks": [],
                "rollback_required": False,
            }
        )
        adapter = remediation_execution.create_approval_execution_adapter()

        with patch("toolsets.remediation_execution.k8s_write.execute_approved", new=execute), patch(
            "toolsets.remediation_execution.operation_lock.acquire_lock",
            new=acquire,
        ), patch("toolsets.remediation_execution.operation_lock.release_lock", new=release), patch(
            "toolsets.remediation_execution.audit_log.record_audit",
            new=record_audit,
        ), patch("toolsets.remediation_execution.incident_store.add_event", new=add_event), patch(
            "toolsets.remediation_execution.remediation_health.check_and_record_action_health",
            new=health,
        ):
            result = await approval_execution.process_approval_execution(approval_id, adapter=adapter)

        execution = await approval_execution.check_execution(approval_id)
        approval = await approval_async.check_approval(approval_id)

        assert result["ok"] is True
        assert result["status"] == "succeeded"
        assert approval["status"] == "executed"
        assert execution is not None
        assert execution["status"] == "succeeded"
        assert execution["dry_run_result"]["command_preview"] == (
            "kubectl scale deployment/nginx --replicas=3 -n default --dry-run=server"
        )
        assert execution["lock_key"] == "k8s:prod-a:default:deployment/nginx"
        assert execution["audit_id"] == 101
        assert execution["health_result"]["status"] == "healthy"
        assert execute.await_count == 2
        assert [call.args[0] for call in execute.await_args_list] == [
            "kubectl scale deployment/nginx --replicas=3 -n default --dry-run=server",
            "kubectl scale deployment/nginx --replicas=3 -n default",
        ]
        assert [call.args[1] for call in execute.await_args_list] == [None, None]
        acquire.assert_awaited_once()
        release.assert_awaited_once()
        record_audit.assert_awaited_once()
        health.assert_awaited_once()

        event_types = [call.args[1] for call in add_event.await_args_list]
        assert "approval_execution_succeeded" not in event_types
        assert all(not event_type.startswith("approval_execution_") for event_type in event_types)
        assert set(event_types).issubset({"remediate_progress", "remediate_executed"})
    finally:
        approval_async._DB.close()
        approval_async._DB = old_db


@pytest.mark.asyncio
async def test_factory_adapter_health_rollback_required_blocks_approval_execution(
    tmp_path,
    **_kwargs,
) -> None:
    from toolsets import approval_async, approval_execution, incident_store, message_delivery, remediation_health

    old_approval_db = approval_async._DB
    old_incident_store = incident_store._STORE
    old_delivery_db = message_delivery._DB
    approval_async._DB = approval_async.ApprovalDB(tmp_path / "approvals.db")
    incident_store._STORE = incident_store.IncidentStore(tmp_path / "incidents.db")
    message_delivery._DB = message_delivery.MessageDeliveryDB(tmp_path / "message_deliveries.db")
    try:
        incident_id = await incident_store.create_incident(
            "DeploymentUnavailable",
            "default",
            "prod-a",
            "nginx unavailable",
            platform="feishu",
            chat_id="oc_ops",
            thread_id="omt_thread",
        )
        await incident_store.update_status(incident_id, "triaging")
        await incident_store.update_status(incident_id, "investigating")
        await incident_store.update_status(incident_id, "pending_approval")

        action = _scale_action()
        action["source"] = {**action["source"], "incident_id": incident_id}
        approval_context = {
            "action_signature": action["action_signature"],
            "executable": True,
            "remediation_action": action,
        }
        approval_id = await approval_async.request_approval(
            "k8s_write",
            "scale deployment/nginx",
            approval_context,
            "default",
            "alert_webhook",
            "low",
            incident_id=incident_id,
        )
        resolved = await approval_async.resolve_approval(approval_id, "approved", "alice")
        assert resolved["ok"] is True

        execute = AsyncMock(side_effect=[
            {
                "ok": True,
                "stdout": "server dry run accepted",
                "stderr": "",
                "exit_code": 0,
            },
            {
                "ok": True,
                "stdout": "deployment.apps/nginx scaled",
                "stderr": "",
                "exit_code": 0,
                "result": {"line_count": 1},
            },
        ])
        acquire = AsyncMock(return_value=True)
        release = AsyncMock(return_value=True)
        record_audit = AsyncMock(return_value=101)
        health = AsyncMock(
            return_value={
                "ok": False,
                "status": "rollback_required",
                "reason_code": "deployment_unavailable",
                "summary": "1/3 replicas available",
                "checks": [],
                "rollback_required": True,
            }
        )
        adapter = remediation_execution.create_approval_execution_adapter(
            health_timeout_seconds=0,
            health_interval_seconds=0,
        )

        with patch("toolsets.remediation_execution.k8s_write.execute_approved", new=execute), patch(
            "toolsets.remediation_execution.operation_lock.acquire_lock",
            new=acquire,
        ), patch("toolsets.remediation_execution.operation_lock.release_lock", new=release), patch(
            "toolsets.remediation_execution.audit_log.record_audit",
            new=record_audit,
        ), patch("toolsets.remediation_health.check_action_health", new=health):
            result = await approval_execution.process_approval_execution(approval_id, adapter=adapter)

        execution = await approval_execution.check_execution(approval_id)
        approval = await approval_async.check_approval(approval_id)
        incident = await incident_store.get_incident(incident_id)
        timeline = await incident_store.get_timeline(incident_id)
        pending = await message_delivery.list_pending()

        assert result["ok"] is False
        assert result["status"] == "rollback_required"
        assert approval["status"] == "approved"
        assert execution is not None
        assert execution["status"] == "rollback_required"
        assert execution["health_result"]["status"] == "rollback_required"
        assert execution["health_result"]["rollback_required_record"]["ok"] is True
        assert incident["status"] == "rollback_required"
        assert timeline[-1]["event_type"] == "rollback_required"
        assert pending[0]["target_type"] == "rollback_required"
        assert pending[0]["approval_id"] == approval_id
        assert health.await_count == 1
        release.assert_awaited_once()
    finally:
        approval_async._DB.close()
        incident_store._STORE.close()
        message_delivery._DB.close()
        approval_async._DB = old_approval_db
        incident_store._STORE = old_incident_store
        message_delivery._DB = old_delivery_db


@pytest.mark.asyncio
async def test_factory_adapter_sends_dry_run_failed_thread_notification(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    **_kwargs,
) -> None:
    from toolsets import approval_async, approval_execution, incident_store, message_delivery

    old_approval_db = approval_async._DB
    old_incident_store = incident_store._STORE
    old_delivery_db = message_delivery._DB
    approval_async._DB = approval_async.ApprovalDB(tmp_path / "approvals.db")
    incident_store._STORE = incident_store.IncidentStore(tmp_path / "incidents.db")
    message_delivery._DB = message_delivery.MessageDeliveryDB(tmp_path / "message_deliveries.db")
    try:
        incident_id = await incident_store.create_incident(
            "DeploymentUnavailable",
            "prod",
            "prod-a",
            "api unavailable",
            platform="feishu",
            chat_id="oc_ops",
            root_message_id="om_root",
            thread_id="omt_thread",
            status_card_message_id="om_card",
        )
        await incident_store.update_status(incident_id, "triaging")
        await incident_store.update_status(incident_id, "investigating")
        await incident_store.update_status(incident_id, "pending_approval")

        action = _restart_action()
        action["source"] = {**action["source"], "incident_id": incident_id}
        approval_context = {
            "action_signature": action["action_signature"],
            "executable": True,
            "remediation_action": action,
        }
        approval_id = await approval_async.request_approval(
            "k8s_write",
            "rollout restart deployment/api",
            approval_context,
            "prod",
            "alert_webhook",
            "low",
            incident_id=incident_id,
        )
        resolved = await approval_async.resolve_approval(approval_id, "approved", "alice")
        assert resolved["ok"] is True

        execute = AsyncMock(return_value={
            "ok": False,
            "stdout": "",
            "stderr": "error: unknown flag: --dry-run",
            "exit_code": 1,
        })
        acquire = AsyncMock()
        publish = AsyncMock(return_value={
            "message_id": "om_dry_run_failed",
            "root_message_id": "om_root",
            "thread_id": "omt_thread",
        })
        monkeypatch.setattr(
            remediation_execution.feishu_conversation,
            "publish_approval_execution_result",
            publish,
            raising=False,
        )

        adapter = remediation_execution.create_approval_execution_adapter(
            health_timeout_seconds=0,
            health_interval_seconds=0,
        )

        with patch("toolsets.remediation_execution.k8s_write.execute_approved", new=execute), patch(
            "toolsets.remediation_execution.operation_lock.acquire_lock",
            new=acquire,
        ):
            result = await approval_execution.process_approval_execution(approval_id, adapter=adapter)

        execution = await approval_execution.check_execution(approval_id)
        sent = await message_delivery.find_sent_delivery_for_approval(
            approval_id=approval_id,
            target_type="approval_execution_dry_run_failed",
        )

        assert result["status"] == "dry_run_failed"
        assert execution is not None
        assert execution["dry_run_result"]["command_preview"] == (
            "kubectl patch deployment/api -n prod --type=strategic -p '{}' --dry-run=server"
        )
        acquire.assert_not_awaited()
        publish.assert_awaited_once()
        assert sent is not None
        assert sent["delivery_status"] == "sent"
        assert sent["target_message_id"] == "om_dry_run_failed"
    finally:
        approval_async._DB.close()
        incident_store._STORE.close()
        message_delivery._DB.close()
        approval_async._DB = old_approval_db
        incident_store._STORE = old_incident_store
        message_delivery._DB = old_delivery_db
