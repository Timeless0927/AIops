from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


def _load_module(tmp_path: Path):
    module_path = Path(__file__).resolve().parents[1] / "toolsets" / "message_delivery.py"
    spec = importlib.util.spec_from_file_location("test_message_delivery_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module._DB.close()
    module._DB = module.MessageDeliveryDB(tmp_path / "message_deliveries.db")
    return module


@pytest.mark.asyncio
async def test_message_delivery_lifecycle(tmp_path: Path, **_: object) -> None:
    module = _load_module(tmp_path)

    delivery_id = await module.upsert_delivery(
        incident_id="incident-1",
        target_type="thread_summary",
        platform="feishu",
        chat_id="oc_ops",
        thread_id="omt_thread",
        payload_hash="hash-1",
    )
    pending = await module.list_pending()
    assert pending[0]["id"] == delivery_id
    assert pending[0]["delivery_status"] == "pending"

    await module.mark_failed(delivery_id, "timeout")
    failed = await module.get_delivery(delivery_id)
    assert failed["delivery_status"] == "failed"
    assert failed["delivery_attempts"] == 1
    assert failed["last_delivery_error"] == "timeout"

    await module.mark_sent(delivery_id, "om_msg")
    sent = await module.get_delivery(delivery_id)
    assert sent["delivery_status"] == "sent"
    assert sent["target_message_id"] == "om_msg"


@pytest.mark.asyncio
async def test_queue_rollback_required_notification(tmp_path: Path, **_: object) -> None:
    module = _load_module(tmp_path)

    queued = await module.queue_rollback_required_notification(
        incident_id="incident-1",
        platform="feishu",
        chat_id="oc_ops",
        thread_id="omt_thread",
        approval_id="approval-1",
        action={
            "action_type": "scale_deployment",
            "namespace": "default",
            "resource_kind": "deployment",
            "resource_name": "nginx",
        },
        health_result={
            "reason_code": "deployment_unavailable",
            "summary": "1/3 replicas available",
        },
    )
    pending = await module.list_pending()

    assert queued["ok"] is True
    assert queued["payload"]["target_type"] == "rollback_required"
    assert "deployment_unavailable" in queued["payload"]["content"]["text"]
    assert pending[0]["id"] == queued["delivery_id"]
    assert pending[0]["target_type"] == "rollback_required"
    assert pending[0]["payload_json"]


@pytest.mark.asyncio
async def test_queue_approval_execution_notifications_are_auditable_by_terminal_status(
    tmp_path: Path,
    **_: object,
) -> None:
    module = _load_module(tmp_path)
    action = {
        "action_type": "restart_deployment",
        "namespace": "default",
        "resource_kind": "deployment",
        "resource_name": "nginx",
    }

    succeeded = await module.queue_approval_execution_notification(
        incident_id="incident-1",
        platform="feishu",
        chat_id="oc_ops",
        thread_id="omt_thread",
        approval_id="approval-1",
        event_type="approval_execution_succeeded",
        approval={"approval_id": "approval-1"},
        execution={"id": "exec-1", "status": "succeeded", "audit_id": 7},
        action=action,
    )
    failed = await module.queue_approval_execution_notification(
        incident_id="incident-1",
        platform="feishu",
        chat_id="oc_ops",
        thread_id="omt_thread",
        approval_id="approval-1",
        event_type="approval_execution_failed",
        approval={"approval_id": "approval-1"},
        execution={"id": "exec-1", "status": "failed", "error_message": "lock not acquired"},
        action=action,
    )
    dry_run_failed = await module.queue_approval_execution_notification(
        incident_id="incident-1",
        platform="feishu",
        chat_id="oc_ops",
        thread_id="omt_thread",
        approval_id="approval-1",
        event_type="approval_execution_dry_run_failed",
        approval={"approval_id": "approval-1"},
        execution={
            "id": "exec-1",
            "status": "dry_run_failed",
            "dry_run_result": {
                "reason_code": "dry_run_failed",
                "summary": "server dry-run failed",
            },
        },
        action=action,
    )
    rollback = await module.queue_approval_execution_notification(
        incident_id="incident-1",
        platform="feishu",
        chat_id="oc_ops",
        thread_id="omt_thread",
        approval_id="approval-1",
        event_type="approval_execution_rollback_required",
        approval={"approval_id": "approval-1"},
        execution={
            "id": "exec-1",
            "status": "rollback_required",
            "health_result": {"reason_code": "deployment_unavailable", "summary": "1/2 replicas ready"},
        },
        action=action,
    )
    pending = await module.list_pending()

    assert [row["target_type"] for row in pending] == [
        "approval_execution_succeeded",
        "approval_execution_failed",
        "approval_execution_dry_run_failed",
        "rollback_required",
    ]
    assert all(row["payload_json"] for row in pending)
    assert succeeded["payload"]["target_type"] == "approval_execution_succeeded"
    assert "自动修复执行成功" in succeeded["payload"]["content"]["text"]
    assert failed["payload"]["target_type"] == "approval_execution_failed"
    assert "lock not acquired" in failed["payload"]["content"]["text"]
    assert dry_run_failed["payload"]["target_type"] == "approval_execution_dry_run_failed"
    assert dry_run_failed["payload"]["metadata"]["reason_code"] == "dry_run_failed"
    assert "Reason: dry_run_failed" in dry_run_failed["payload"]["content"]["text"]
    dry_run_delivery = pending[2]
    dry_run_payload = json.loads(dry_run_delivery["payload_json"])
    assert dry_run_payload["metadata"]["reason_code"] == "dry_run_failed"
    assert "未执行真实变更" in dry_run_payload["content"]["text"]
    assert rollback["payload"]["target_type"] == "rollback_required"
    assert "deployment_unavailable" in rollback["payload"]["content"]["text"]
