"""基于事件时间线生成 Skill 草稿。"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

try:
    import langextract as lx
    LANGEXTRACT_AVAILABLE = True
except ImportError:  # pragma: no cover - 依赖可选
    lx = None
    LANGEXTRACT_AVAILABLE = False

try:
    from tools.registry import registry
except ImportError:  # pragma: no cover - 测试环境兼容
    from hermes_agent.tools.registry import registry  # type: ignore

from toolsets import incident_store


def _project_root() -> Path:
    """返回项目根目录。"""
    return Path(__file__).resolve().parent.parent


def _skills_root() -> Path:
    """返回 SRE skills 根目录。"""
    return _project_root() / "skills" / "sre"


def _draft_dir(incident_id: str) -> Path:
    """返回草稿目录。"""
    return _skills_root() / "drafts" / incident_id


def _timeline_to_text(incident_id: str, timeline: list[dict[str, Any]]) -> str:
    """将时间线转为可供模型分析的纯文本。"""
    lines = [f"事件 ID: {incident_id}"]
    for item in timeline:
        lines.append(
            " | ".join(
                [
                    f"event_type={item.get('event_type', '')}",
                    f"tool_name={item.get('tool_name', '')}",
                    f"input={item.get('input_summary', '')}",
                    f"output={item.get('output_summary', '')}",
                ]
            )
        )
    return "\n".join(lines)


def _extract_section(title: str, text: str) -> list[str]:
    """从模型文本中抽取某一节内容。"""
    lines = text.splitlines()
    captured: list[str] = []
    collecting = False
    for line in lines:
        normalized = line.strip()
        if normalized.startswith("## "):
            collecting = normalized == f"## {title}"
            continue
        if collecting:
            captured.append(line)
    return [line for line in captured if line.strip()]


def _fallback_lines(timeline: list[dict[str, Any]], event_types: set[str]) -> list[str]:
    """基于事件类型筛选降级内容。"""
    results: list[str] = []
    for item in timeline:
        if item.get("event_type") in event_types:
            tool_name = item.get("tool_name") or "未知工具"
            summary = item.get("output_summary") or item.get("input_summary") or "无摘要"
            results.append(f"- {tool_name}: {summary}")
    return results


def _fallback_markdown(incident_id: str, timeline: list[dict[str, Any]]) -> str:
    """在模型不可用时直接用时间线填充模板。"""
    trigger_lines = _fallback_lines(timeline, {"alert_fired", "triage_start", "triage_end"}) or ["- 触发条件待补充：请根据告警信息和时间线补全。"]
    diagnose_lines = _fallback_lines(timeline, {"triage_start", "triage_end", "investigate_start", "investigate_end"}) or ["- 诊断步骤待补充：请根据时间线中的工具调用补全。"]
    remediate_lines = _fallback_lines(timeline, {"remediate_proposed", "remediate_executed"}) or ["- 修复方案待补充：请根据实际执行动作补全。"]
    verify_lines = _fallback_lines(timeline, {"remediate_verified", "resolved"}) or ["- 验证步骤待补充：请根据验证记录补全。"]
    root_cause = next((item.get("output_summary") for item in timeline if item.get("event_type") == "investigate_end" and item.get("output_summary")), "- 根因待人工总结：当前根据时间线未能自动归纳。")

    sections = [
        f"# 事件 {incident_id} Runbook 草稿",
        "",
        "## 场景描述",
        "根据该事件时间线自动生成的运行手册草稿，需人工审核后上线。",
        "",
        "## 触发条件",
        *trigger_lines,
        "",
        "## 诊断步骤",
        *diagnose_lines,
        "",
        "## 常见根因",
        root_cause if root_cause.startswith("-") else f"- {root_cause}",
        "",
        "## 修复方案",
        *remediate_lines,
        "",
        "## 验证步骤",
        *verify_lines,
    ]
    return "\n".join(sections).strip() + "\n"


async def _analyze_with_llm(incident_id: str, timeline: list[dict[str, Any]]) -> str:
    """调用便宜模型抽取可复用步骤。"""
    if not LANGEXTRACT_AVAILABLE or lx is None:
        raise RuntimeError("langextract 不可用")

    model_id = os.environ.get("EXTRACTOR_MODEL", "gemini-1.5-flash")
    timeline_text = _timeline_to_text(incident_id, timeline)
    prompt = (
        "你是 SRE 运行手册整理助手。"
        "请根据事件时间线输出 Markdown，且必须严格包含以下二级标题："
        "## 场景描述、## 触发条件、## 诊断步骤、## 常见根因、## 修复方案、## 验证步骤。"
        "内容使用中文，尽量抽象成可复用步骤。"
    )

    def _run_extract() -> str:
        result = lx.extract(
            text_or_documents=timeline_text,
            prompt_description=prompt,
            model_id=model_id,
            max_workers=1,
            extraction_passes=1,
            max_char_buffer=4000,
        )
        raw_text = getattr(result, "text", "") or getattr(result, "output_text", "")
        if raw_text:
            return str(raw_text)

        extractions = getattr(result, "extractions", []) or []
        lines = []
        for ext in extractions:
            ext_class = getattr(ext, "extraction_class", "信息")
            ext_text = getattr(ext, "extraction_text", "")
            lines.append(f"- {ext_class}: {ext_text}")
        if not lines:
            raise RuntimeError("模型未返回可用内容")
        return "\n".join(lines)

    return await asyncio.to_thread(_run_extract)


def _compose_markdown(incident_id: str, llm_text: str, timeline: list[dict[str, Any]]) -> str:
    """将模型输出整理为统一 runbook 结构。"""
    sections = {
        "场景描述": _extract_section("场景描述", llm_text),
        "触发条件": _extract_section("触发条件", llm_text),
        "诊断步骤": _extract_section("诊断步骤", llm_text),
        "常见根因": _extract_section("常见根因", llm_text),
        "修复方案": _extract_section("修复方案", llm_text),
        "验证步骤": _extract_section("验证步骤", llm_text),
    }
    if not any(sections.values()):
        return _fallback_markdown(incident_id, timeline)

    parts = [f"# 事件 {incident_id} Runbook 草稿", ""]
    for title in ("场景描述", "触发条件", "诊断步骤", "常见根因", "修复方案", "验证步骤"):
        content = sections[title] or [f"- {title}待人工补充。"]
        parts.extend([f"## {title}", *content, ""])
    return "\n".join(parts).strip() + "\n"


async def extract_skill_draft(incident_id: str) -> dict[str, Any]:
    """根据事件时间线生成 Skill 草稿文件。"""
    timeline = await incident_store.get_timeline(incident_id)
    if not timeline:
        return {"ok": False, "message": f"未找到事件时间线: {incident_id}"}

    method = "fallback"
    try:
        llm_text = await _analyze_with_llm(incident_id, timeline)
        markdown = _compose_markdown(incident_id, llm_text, timeline)
        method = "llm"
    except Exception:
        markdown = _fallback_markdown(incident_id, timeline)

    draft_dir = _draft_dir(incident_id)
    draft_dir.mkdir(parents=True, exist_ok=True)
    draft_path = draft_dir / "SKILL.md"
    await asyncio.to_thread(draft_path.write_text, markdown, encoding="utf-8")

    return {
        "ok": True,
        "incident_id": incident_id,
        "path": str(draft_path),
        "method": method,
    }


SKILL_EXTRACTOR_SCHEMA = {
    "name": "skill_extractor",
    "description": "从事件时间线提取可复用模式并生成 Skill 草稿。",
    "parameters": {
        "type": "object",
        "properties": {
            "incident_id": {"type": "string", "description": "事件 ID"},
        },
        "required": ["incident_id"],
    },
}


async def _tool_skill_extractor(args: dict[str, Any], **_: Any) -> str:
    """工具入口：生成 Skill 草稿。"""
    return json.dumps(await extract_skill_draft(args.get("incident_id", "")), ensure_ascii=False)


registry.register(name="skill_extractor", toolset="sre", schema=SKILL_EXTRACTOR_SCHEMA, handler=_tool_skill_extractor, is_async=True)
