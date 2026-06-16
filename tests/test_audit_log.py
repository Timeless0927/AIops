"""测试审计日志模块。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_module(tmp_path: Path):
    """按文件路径加载模块，并重定向数据库路径。"""
    module_path = Path(__file__).resolve().parents[1] / "toolsets" / "audit_log.py"
    spec = importlib.util.spec_from_file_location("test_audit_log_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)

    db = module.AuditLogDB(tmp_path / "data" / "audit_log.db")
    old_db = module._DB
    old_db.close()
    module._DB = db
    return module, db


@pytest.mark.asyncio
async def test_record_and_query_audit(tmp_path: Path, **_: object) -> None:
    """验证审计写入和多条件查询。"""
    module, db = _load_module(tmp_path)
    await module.record_audit("feishu:ou_1", "查看 pod", "prod", "default", "manual", "read", "k8s_read", "success")
    mid = await module.record_audit(
        "feishu:ou_2",
        "执行修复",
        "prod",
        "ops",
        "alert",
        "write",
        "k8s_write",
        "failed",
        incident_id="inc-1",
        actor="ou_2",
        role="oncall_approver",
        scope={"services": ["checkout"], "teams": ["payments"], "namespaces": ["ops"]},
        request_id="req-1",
    )
    await module.record_audit("feishu:ou_1", "查询指标", "test", "default", "cron", "read", "prometheus_query", "success", incident_id="inc-1")

    all_rows = await module.query_audit(limit=10)
    assert len(all_rows) == 3

    time_start = all_rows[-1]["when_ts"]
    time_end = all_rows[0]["when_ts"]
    filtered = await module.query_audit(time_start=time_start, time_end=time_end, who="feishu:ou_2", cluster="prod", namespace="ops", limit=10)
    assert len(filtered) == 1
    assert filtered[0]["id"] == mid
    assert filtered[0]["actor"] == "ou_2"
    assert filtered[0]["role"] == "oncall_approver"
    assert filtered[0]["scope"] == '{"namespaces": ["ops"], "services": ["checkout"], "teams": ["payments"]}'
    assert filtered[0]["request_id"] == "req-1"

    by_incident = await module.query_audit_by_incident("inc-1")
    assert len(by_incident) == 2

    db.close()


@pytest.mark.asyncio
async def test_query_empty_result(tmp_path: Path, **_: object) -> None:
    """空库查询应返回空列表。"""
    module, db = _load_module(tmp_path)
    result = await module.query_audit(who="nobody")
    incident_result = await module.query_audit_by_incident("missing")
    assert result == []
    assert incident_result == []
    db.close()
