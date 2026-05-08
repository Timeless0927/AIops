from __future__ import annotations

import importlib.util
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
