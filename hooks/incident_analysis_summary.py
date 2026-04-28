"""incident 分析摘要渲染。"""

from __future__ import annotations

from typing import Any


_NO_EVIDENCE = "暂无结构化证据，请先补充 Pod 状态、事件或日志摘要"
_NO_CAUSE = "暂未形成明确根因候选"
_NO_ACTION = "继续补充关键证据后再更新结论"


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
    return [_NO_EVIDENCE]


def _cause_lines(analysis: dict[str, Any]) -> list[str]:
    lines = [item.strip() for item in _analysis_list(analysis, "suspected_root_causes") if isinstance(item, str) and item.strip()]
    if lines:
        return lines
    return [_NO_CAUSE]


def _action_lines(analysis: dict[str, Any]) -> list[str]:
    lines = [item.strip() for item in _analysis_list(analysis, "next_best_actions") if isinstance(item, str) and item.strip()]
    if lines:
        return lines
    return [_NO_ACTION]


def _current_judgement(alert: dict[str, Any], has_analysis: bool) -> str:
    namespace = str(alert.get("namespace") or "unknown")
    cluster = str(alert.get("cluster") or "unknown")
    alertname = str(alert.get("alertname") or "当前告警")
    if has_analysis:
        return f"{namespace}/{cluster} 的 {alertname} 已有初步结论，仍需在线程内持续跟进。"
    return f"{namespace}/{cluster} 的 {alertname} 仍在补充证据，暂未形成稳定结论。"


def _bullet_block(lines: list[str]) -> str:
    return "\n".join(f"- {line}" for line in lines)


def render_thread_summary(
    incident: dict[str, Any],
    alert: dict[str, Any],
    analysis: dict[str, Any],
    evidence_rows: list[Any],
) -> str:
    """渲染固定 MVP 线程摘要。"""
    del incident
    effective_analysis = analysis if isinstance(analysis, dict) else {}
    if evidence_rows:
        effective_analysis = dict(effective_analysis)
        effective_analysis["supporting_evidence"] = evidence_rows

    evidence_lines = _evidence_lines(effective_analysis)
    cause_lines = _cause_lines(effective_analysis)
    action_lines = _action_lines(effective_analysis)
    has_analysis = not (
        evidence_lines == [_NO_EVIDENCE] and cause_lines == [_NO_CAUSE] and action_lines == [_NO_ACTION]
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


def render_context_summary(incident: dict[str, Any], analysis: dict[str, Any]) -> str:
    """渲染供后续上下文复用的紧凑摘要。"""
    incident_id = str(incident.get("id") or "unknown")
    status = str(incident.get("status") or "unknown")
    cause = _cause_lines(analysis if isinstance(analysis, dict) else {})[0]
    action = _action_lines(analysis if isinstance(analysis, dict) else {})[0]
    return (
        f"Incident {incident_id} 当前状态: {status}\n"
        f"根因候选: {cause}\n"
        f"建议下一步: {action}"
    )
