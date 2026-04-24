"""测试异步审批模块。"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest


def _load_module(tmp_path: Path):
    """按文件路径加载模块，并重定向数据库路径。"""
    module_path = Path(__file__).resolve().parents[1] / "toolsets" / "approval_async.py"
    spec = importlib.util.spec_from_file_location("test_approval_async_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module._DB.close()
    module._DB = module.ApprovalDB(tmp_path / "approvals.db")
    return module


@pytest.mark.asyncio
async def test_approval_lifecycle(tmp_path: Path, **_kwargs) -> None:
    """审批应支持完整生命周期。"""
    module = _load_module(tmp_path)

    approval_id = await module.request_approval(
        "k8s_write",
        "kubectl apply -f deploy.yaml",
        {"env": "prod"},
        "default",
        "alice",
        "standard",
    )
    checked = await module.check_approval(approval_id)
    resolved = await module.resolve_approval(approval_id, "approved", "bob")
    executed = await module.execute_approved(approval_id)
    final_state = await module.check_approval(approval_id)

    assert checked["status"] == "pending"
    assert resolved["status"] == "approved"
    assert executed["status"] == "executed"
    assert final_state["status"] == "executed"
    assert final_state["approver"] == "bob"
    assert final_state["result"]["executed"] is True


@pytest.mark.asyncio
async def test_expire_stale_pending_approval(tmp_path: Path, **_kwargs) -> None:
    """超时未处理审批应被标记为 expired。"""
    module = _load_module(tmp_path)

    approval_id = await module.request_approval(
        "k8s_exec",
        "kubectl exec pod/app -- sh",
        {},
        "ops",
        "alice",
        "elevated",
    )
    module._DB._conn.execute("UPDATE approvals SET created_at = ? WHERE id = ?", (0, approval_id))
    expired = await module.expire_stale(timeout_minutes=30)
    checked = await module.check_approval(approval_id)

    assert expired["ok"] is True
    assert expired["expired"] == 1
    assert checked["status"] == "expired"


@pytest.mark.asyncio
async def test_denied_flow_cannot_execute(tmp_path: Path, **_kwargs) -> None:
    """被拒绝的审批不允许执行。"""
    module = _load_module(tmp_path)

    approval_id = await module.request_approval(
        "k8s_write",
        "kubectl delete deployment web",
        {},
        "prod",
        "alice",
        "dangerous",
    )
    resolved = await module.resolve_approval(approval_id, "denied", "bob")
    executed = await module.execute_approved(approval_id)

    assert resolved["status"] == "denied"
    assert executed["ok"] is False
    assert "当前状态不允许执行" in executed["message"]


@pytest.mark.asyncio
async def test_approval_records_incident_and_message_ids(tmp_path: Path, **_kwargs) -> None:
    """审批记录应关联 incident 与飞书审批消息。"""
    module = _load_module(tmp_path)

    approval_id = await module.request_approval(
        "k8s_write",
        "kubectl rollout restart deployment/web -n prod",
        {"resource": "deployment/web"},
        "prod",
        "alice",
        "dangerous",
        incident_id="incident-1",
        approval_message_id="om_approval",
    )
    checked = await module.check_approval(approval_id)

    assert checked["incident_id"] == "incident-1"
    assert checked["approval_message_id"] == "om_approval"


@pytest.mark.asyncio
async def test_concurrent_requests_generate_distinct_ids(tmp_path: Path, **_kwargs) -> None:
    """并发创建审批请求时应得到不同 ID。"""
    module = _load_module(tmp_path)

    async def _create_one(index: int) -> str:
        return await module.request_approval(
            "k8s_write",
            f"kubectl scale deployment/app --replicas={index}",
            {"index": index},
            "default",
            f"user-{index}",
            "standard",
        )

    ids = await asyncio.gather(*[_create_one(index) for index in range(10)])

    assert len(ids) == 10
    assert len(set(ids)) == 10
