"""测试规则引擎降级模块。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_module():
    """按文件路径加载模块。"""
    module_path = Path(__file__).resolve().parents[1] / "toolsets" / "llm_fallback.py"
    spec = importlib.util.spec_from_file_location("test_llm_fallback_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_exact_alert_match(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "_load_fallback_rules", lambda: [{"alert": "KubePodCrashLooping", "action": "kubectl describe pod {pod} -n {namespace}", "deliver": "origin"}])
    result = await module.match_fallback_rule("KubePodCrashLooping", {"pod": "web-0", "namespace": "default"})
    assert result == {"alert": "KubePodCrashLooping", "action": "kubectl describe pod web-0 -n default", "deliver": "origin"}


@pytest.mark.asyncio
async def test_default_rule_used_when_no_match(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "_load_fallback_rules", lambda: [{"default": True, "action": "forward_raw"}])
    result = await module.match_fallback_rule("UnknownAlert", {})
    assert result == {"alert": "UnknownAlert", "action": "forward_raw", "deliver": None}


@pytest.mark.asyncio
async def test_template_variable_replacement(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "_load_fallback_rules", lambda: [{"alert": "NodeNotReady", "action": "node={node} cluster={cluster}", "deliver": "origin"}])
    result = await module.match_fallback_rule("NodeNotReady", {"node": "node-a", "cluster": "prod"})
    assert result["action"] == "node=node-a cluster=prod"


@pytest.mark.asyncio
async def test_none_when_no_default_and_no_match(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "_load_fallback_rules", lambda: [{"alert": "Other", "action": "noop"}])
    result = await module.match_fallback_rule("Missing", {})
    assert result is None


def test_format_degradation_notice() -> None:
    module = _load_module()
    assert module.format_degradation_notice("provider_timeout") == "⚠️ AI 诊断暂时不可用（provider_timeout），已切换到规则引擎模式。"

