"""测试 AIOps SRE 工具注册。"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


EXPECTED_TOOLS = {
    "incident_create",
    "incident_add_event",
    "incident_timeline",
    "incident_list_active",
    "sre_request_approval",
    "sre_check_approval",
    "sre_resolve_approval",
    "sre_audit_record",
    "sre_audit_query",
    "alert_dedup_status",
    "sre_acquire_lock",
    "sre_release_lock",
    "sre_check_lock",
    "sre_check_permission",
    "k8s_read",
    "k8s_write",
    "k8s_exec",
    "sre_shift_handoff",
    "skill_extractor",
    "skill_list_drafts",
    "skill_promote_draft",
    "skill_discard_draft",
    "sre_notification_check",
    "sre_notification_digest",
    "sre_fallback_match",
    "sre_health_check",
    "sre_record_rejection",
    "sre_rejection_stats",
    "sre_cost_record",
    "sre_cost_check",
    "sre_metrics",
    "sre_weekly_summary",
    "sre_voice_summary",
}

TOOL_MODULES = (
    "toolsets.incident_store",
    "toolsets.approval_async",
    "toolsets.audit_log",
    "toolsets.alert_dedup",
    "toolsets.operation_lock",
    "toolsets.permission_guard",
    "toolsets.k8s_read",
    "toolsets.k8s_write",
    "toolsets.k8s_exec",
    "toolsets.shift_handoff",
    "toolsets.skill_extractor_tool",
    "toolsets.skill_promotion",
    "toolsets.notification_manager",
    "toolsets.llm_fallback",
    "hooks.health_check",
    "toolsets.rejection_learner",
    "toolsets.cost_guard",
    "toolsets.sre_metrics",
    "toolsets.voice_summary",
)


def _load_registry():
    """加载本地工具注册器。"""
    from tools.registry import registry

    return registry


def _import_tool_modules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """导入本仓库工具模块并隔离其默认数据目录。"""
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path / "data"))
    for module_name in TOOL_MODULES:
        importlib.import_module(module_name)


def test_tool_module_list_matches_expected_contract() -> None:
    """注册测试覆盖所有预期 SRE 工具模块。"""
    assert len(TOOL_MODULES) == 19
    assert EXPECTED_TOOLS


def test_expected_tools_are_registered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """导入工具模块后，所有预期工具都应出现在 registry 中。"""
    registry = _load_registry()
    _import_tool_modules(tmp_path, monkeypatch)

    missing = {name for name in EXPECTED_TOOLS if registry.get_entry(name) is None}
    assert not missing, f"未注册工具: {sorted(missing)}"
