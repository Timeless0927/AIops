"""incident 分析摘要渲染。"""

from __future__ import annotations

from typing import Any


def _analysis_list(analysis: dict[str, Any], key: str) -> list[Any]:
    value = analysis.get(key)
    return value if isinstance(value, list) else []


def _evidence_lines(analysis: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for item in _analysis_list(analysis, "supporting_evidence"):
        if isinstance(item, dict):
            summary = item.get("summary")
        else:
            summary = item
        if isinstance(summary, str) and summary.strip():
            lines.append(summary.strip())
    if lines:
        return lines
    return ["暂无结构化证据，请先补充 Pod 状态、事件或日志摘要"]


def _cause_lines(analysis: dict[str, Any]) -> list[str]:
    lines = [item.strip() for item in _analysis_list(analysis, "suspected_root_causes") if isinstance(item, str) and item.strip()]
    if lines:
        return lines
    return ["暂未形成明确根因候选"]


def _action_lines(analysis: dict[str, Any]) -> list[str]:
    lines = [item.strip() for item in _analysis_list(analysis, "next_best_actions") if isinstance(item, str) and item.strip()]
    if lines:
        return lines
    return ["继续补充关键证据后再更新结论"]


def _current_judgement(alert: dict[str, Any], has_analysis: bool) -> str:
    namespace = str(alert.get("namespace") or "unknown")
    cluster = str(alert.get("cluster") or "unknown")
    alertname = str(alert.get("alertname") or "当前告警")
    if has_analysis:
        return f"{namespace}/{cluster} 的 {alertname} 已有初步结论，仍需在线程内持续跟进。"
    return f"{namespace}/{cluster} 的 {alertname} 仍在补充证据，暂未形成稳定结论。"


def _bullet_block(lines: list[str]) -> str:
    return "\n".join(f"- {line}" for line in lines)


def render_thread_summary(alert: dict[str, Any]) -> str:
    """渲染固定 MVP 线程摘要。"""
    analysis = alert.get("analysis") if isinstance(alert.get("analysis"), dict) else {}
    evidence_lines = _evidence_lines(analysis)
    cause_lines = _cause_lines(analysis)
    action_lines = _action_lines(analysis)
    has_analysis = not (
        evidence_lines == ["暂无结构化证据，请先补充 Pod 状态、事件或日志摘要"]
        and cause_lines == ["暂未形成明确根因候选"]
        and action_lines == ["继续补充关键证据后再更新结论"]
    )

    return (
        "【当前判断】\n"
        f"{_current_judgement(alert, has_analysis)}\n\n"
        "【关键证据】\n"
        f"{_bullet_block(evidence_lines)}\n\n"
        "【根因候选】\n"
        f"{_bullet_block(cause_lines)}\n\n"
        "【建议下一步】\n"
        f"{_bullet_block(action_lines)}"
    )
