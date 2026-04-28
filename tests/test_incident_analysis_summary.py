"""测试 incident 分析摘要渲染。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_module():
    """按文件路径加载模块。"""
    module_path = Path(__file__).resolve().parents[1] / "hooks" / "incident_analysis_summary.py"
    module_name = "test_incident_analysis_summary_module"
    if module_name in sys.modules:
        del sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_render_thread_summary_outputs_fixed_mvp_sections() -> None:
    """摘要渲染应输出固定 MVP 结构，并复用已有 analysis 字段。"""
    module = _load_module()

    text = module.render_thread_summary(
        {
            "alertname": "PodCrashLooping",
            "namespace": "payments",
            "cluster": "prod-a",
            "analysis": {
                "supporting_evidence": [
                    {"kind": "pod_status", "summary": "payments-api-6f6d 重启 7 次，状态 CrashLoopBackOff"},
                    {"kind": "pod_logs", "summary": "最近日志包含 OOMKilled"},
                ],
                "suspected_root_causes": ["容器内存不足导致 OOMKilled"],
                "next_best_actions": ["先提升内存 limit 并观察 10 分钟"],
            },
        }
    )

    assert text == (
        "【当前判断】\n"
        "payments/prod-a 的 PodCrashLooping 已有初步结论，仍需在线程内持续跟进。\n\n"
        "【关键证据】\n"
        "- payments-api-6f6d 重启 7 次，状态 CrashLoopBackOff\n"
        "- 最近日志包含 OOMKilled\n\n"
        "【根因候选】\n"
        "- 容器内存不足导致 OOMKilled\n\n"
        "【建议下一步】\n"
        "- 先提升内存 limit 并观察 10 分钟"
    )


def test_render_thread_summary_uses_safe_fallbacks_when_analysis_is_sparse() -> None:
    """缺少结构化分析时也应渲染固定四段，避免空白回复。"""
    module = _load_module()

    text = module.render_thread_summary(
        {
            "alertname": "PodCrashLooping",
            "namespace": "default",
            "cluster": "prod-a",
            "analysis": {
                "supporting_evidence": [],
                "suspected_root_causes": [],
                "next_best_actions": [],
            },
        }
    )

    assert "【当前判断】" in text
    assert "【关键证据】\n- 暂无结构化证据，请先补充 Pod 状态、事件或日志摘要" in text
    assert "【根因候选】\n- 暂未形成明确根因候选" in text
    assert "【建议下一步】\n- 继续补充关键证据后再更新结论" in text
