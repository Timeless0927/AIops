"""测试 Skill 提取引擎。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_module(tmp_path: Path):
    """按文件路径加载模块，并将草稿目录重定向到临时目录。"""
    module_path = Path(__file__).resolve().parents[1] / "toolsets" / "skill_extractor_tool.py"
    spec = importlib.util.spec_from_file_location("test_skill_extractor_tool_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)

    skills_root = tmp_path / "skills" / "sre"
    (skills_root / "drafts").mkdir(parents=True, exist_ok=True)
    module._skills_root = lambda: skills_root
    return module, skills_root


def test_load_module_when_hermes_toolsets_shadows_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """即使 hermes-agent/toolsets.py 排在前面，也应加载本项目 incident_store。"""
    hermes_agent_root = Path(__file__).resolve().parents[1] / "hermes-agent"
    monkeypatch.syspath_prepend(str(hermes_agent_root))
    sys.modules.pop("toolsets", None)

    module, _skills_root = _load_module(tmp_path)

    assert hasattr(module.incident_store, "get_timeline")


@pytest.mark.asyncio
async def test_extract_skill_draft_with_mocked_llm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """验证模型可用时会输出统一格式的草稿。"""
    module, skills_root = _load_module(tmp_path)
    incident_id = "incident-llm"
    timeline = [
        {"event_type": "alert_fired", "tool_name": "alert", "input_summary": "PodCrash", "output_summary": "告警触发"},
        {"event_type": "investigate_end", "tool_name": "loki_query", "input_summary": "查日志", "output_summary": "发现 OOM"},
        {"event_type": "remediate_executed", "tool_name": "k8s_write", "input_summary": "扩容内存", "output_summary": "已调整 limits"},
        {"event_type": "resolved", "tool_name": "prometheus_query", "input_summary": "验证指标", "output_summary": "指标恢复"},
    ]

    async def _fake_timeline(value: str):
        assert value == incident_id
        return timeline

    async def _fake_analyze(value: str, items):
        assert value == incident_id
        assert items == timeline
        return """## 场景描述
Pod 因内存不足反复重启。
## 触发条件
- Pod CrashLoopBackOff 告警触发
## 诊断步骤
- 查看容器日志确认 OOM
## 常见根因
- 内存 limit 过低
## 修复方案
- 提高 deployment 内存限制
## 验证步骤
- 检查重启次数和内存指标
"""

    monkeypatch.setattr(module.incident_store, "get_timeline", _fake_timeline)
    monkeypatch.setattr(module, "_analyze_with_llm", _fake_analyze)

    result = await module.extract_skill_draft(incident_id)
    draft_path = skills_root / "drafts" / incident_id / "SKILL.md"
    content = draft_path.read_text(encoding="utf-8")

    assert result["ok"] is True
    assert result["method"] == "llm"
    assert draft_path.exists()
    assert "## 场景描述" in content
    assert "## 验证步骤" in content
    assert "提高 deployment 内存限制" in content


@pytest.mark.asyncio
async def test_extract_skill_draft_fallback_when_llm_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """验证模型不可用时会降级为模板填充。"""
    module, skills_root = _load_module(tmp_path)
    incident_id = "incident-fallback"
    timeline = [
        {"event_type": "alert_fired", "tool_name": "alert", "input_summary": "PVC 使用率 95%", "output_summary": "PVC 告警"},
        {"event_type": "triage_end", "tool_name": "k8s_read", "input_summary": "查看 pvc", "output_summary": "确认容量接近上限"},
        {"event_type": "remediate_executed", "tool_name": "k8s_write", "input_summary": "patch pvc", "output_summary": "扩容到 200Gi"},
        {"event_type": "resolved", "tool_name": "prometheus_query", "input_summary": "验证", "output_summary": "使用率下降"},
    ]

    async def _fake_timeline(value: str):
        assert value == incident_id
        return timeline

    async def _raise_analyze(value: str, items):
        del value, items
        raise RuntimeError("llm 不可用")

    monkeypatch.setattr(module.incident_store, "get_timeline", _fake_timeline)
    monkeypatch.setattr(module, "_analyze_with_llm", _raise_analyze)

    result = await module.extract_skill_draft(incident_id)
    draft_path = skills_root / "drafts" / incident_id / "SKILL.md"
    content = draft_path.read_text(encoding="utf-8")

    assert result["ok"] is True
    assert result["method"] == "fallback"
    assert "## 修复方案" in content
    assert "扩容到 200Gi" in content
    assert "## 验证步骤" in content
