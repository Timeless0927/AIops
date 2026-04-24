"""测试拒绝学习模块。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_module(tmp_path: Path):
    """按文件路径加载模块，并重定向数据文件。"""
    module_path = Path(__file__).resolve().parents[1] / "toolsets" / "rejection_learner.py"
    spec = importlib.util.spec_from_file_location("test_rejection_learner_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    module._lessons_path = lambda: tmp_path / "rejection_lessons.json"
    module.approval_async._DB.close()
    module.approval_async._DB = module.approval_async.ApprovalDB(tmp_path / "approvals.db")
    return module


@pytest.mark.asyncio
async def test_record_rejection_lesson_formats_text(tmp_path: Path, **_: object) -> None:
    """拒绝经验应按规范格式写入。"""
    module = _load_module(tmp_path)
    approval_id = await module.approval_async.request_approval("k8s_write", "kubectl delete pod a", {}, "production", "alice", "dangerous")
    await module.approval_async.resolve_approval(approval_id, "denied", "bob", "风险过高")

    result = await module.record_rejection_lesson(approval_id, "风险过高", {"operation_type": "k8s_write", "namespace": "production"})

    assert result["lesson_id"] == 1
    assert result["lesson_text"] == "在 production 环境下不应该 k8s_write，因为 风险过高"


@pytest.mark.asyncio
async def test_get_lessons_returns_recent_records(tmp_path: Path, **_: object) -> None:
    """应返回最近写入的经验。"""
    module = _load_module(tmp_path)
    approval_id = await module.approval_async.request_approval("k8s_exec", "kubectl exec pod/a -- sh", {}, "default", "alice", "elevated")
    await module.approval_async.resolve_approval(approval_id, "denied", "bob", "不允许执行")
    await module.record_rejection_lesson(approval_id, "不允许执行", {"operation_type": "k8s_exec", "namespace": "default"})

    lessons = await module.get_lessons(limit=5)

    assert len(lessons) == 1
    assert lessons[0]["approval_id"] == approval_id


@pytest.mark.asyncio
async def test_get_rejection_stats_calculates_ratio(tmp_path: Path, **_: object) -> None:
    """应按 operation_type 统计拒绝率。"""
    module = _load_module(tmp_path)

    approval_1 = await module.approval_async.request_approval("k8s_write", "cmd1", {}, "default", "alice", "standard")
    approval_2 = await module.approval_async.request_approval("k8s_write", "cmd2", {}, "default", "alice", "standard")
    approval_3 = await module.approval_async.request_approval("k8s_exec", "cmd3", {}, "default", "alice", "elevated")
    await module.approval_async.resolve_approval(approval_1, "approved", "bob")
    await module.approval_async.resolve_approval(approval_2, "denied", "bob", "危险")
    await module.approval_async.resolve_approval(approval_3, "denied", "bob", "高危")

    result = await module.get_rejection_stats(days=30)

    stats = {item["operation_type"]: item for item in result["stats"]}
    assert stats["k8s_write"]["total"] == 2
    assert stats["k8s_write"]["denied"] == 1
    assert stats["k8s_write"]["ratio"] == 0.5


@pytest.mark.asyncio
async def test_high_rejection_types_are_marked(tmp_path: Path, **_: object) -> None:
    """拒绝率大于 30% 的类型应被标记。"""
    module = _load_module(tmp_path)

    approval_1 = await module.approval_async.request_approval("k8s_exec", "cmd1", {}, "default", "alice", "elevated")
    approval_2 = await module.approval_async.request_approval("k8s_exec", "cmd2", {}, "default", "alice", "elevated")
    await module.approval_async.resolve_approval(approval_1, "denied", "bob", "高危")
    await module.approval_async.resolve_approval(approval_2, "approved", "bob")

    result = await module.get_rejection_stats(days=30)

    assert "k8s_exec" in result["high_rejection_types"]


@pytest.mark.asyncio
async def test_empty_lessons_returns_empty_list(tmp_path: Path, **_: object) -> None:
    """无数据时应返回空列表。"""
    module = _load_module(tmp_path)

    lessons = await module.get_lessons(limit=10)

    assert lessons == []
