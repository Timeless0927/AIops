"""测试 Hermes SRE 插件加载。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml


PLUGIN_ROOT = Path("/home/mao/.hermes/plugins/sre")
PLUGIN_INIT = PLUGIN_ROOT / "__init__.py"
PLUGIN_MANIFEST = PLUGIN_ROOT / "plugin.yaml"
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


def _load_plugin_module():
    """按文件路径加载插件入口模块。"""
    module_name = "test_sre_plugin_module"
    if module_name in sys.modules:
        del sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_INIT)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_registry():
    """加载 Hermes 工具注册器。"""
    hermes_root = Path("/home/mao/aiops/hermes-agent")
    if str(hermes_root) not in sys.path:
        sys.path.insert(0, str(hermes_root))
    from tools.registry import registry

    return registry


def test_plugin_manifest_can_be_parsed() -> None:
    """plugin.yaml 应可正常解析。"""
    data = yaml.safe_load(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
    assert data["name"] == "sre"
    assert set(data["provides_tools"]) == EXPECTED_TOOLS


def test_register_executes_successfully() -> None:
    """register(ctx) 应可执行且不抛异常。"""
    module = _load_plugin_module()
    ctx = object()
    module.register(ctx)


def test_expected_tools_are_registered() -> None:
    """插件注册后，所有预期工具都应出现在 registry 中。"""
    module = _load_plugin_module()
    registry = _load_registry()
    module.register(object())

    missing = {name for name in EXPECTED_TOOLS if registry.get_entry(name) is None}
    assert not missing, f"未注册工具: {sorted(missing)}"
