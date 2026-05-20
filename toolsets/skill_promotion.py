"""Skill 草稿审核与上线工具。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any

import yaml

try:
    from tools.registry import registry
except ImportError:  # pragma: no cover - 测试环境兼容
    from hermes_agent.tools.registry import registry  # type: ignore


def _project_root() -> Path:
    """返回项目根目录。"""
    return Path(__file__).resolve().parent.parent


def _runtime_config_candidates() -> list[Path]:
    """返回运行时配置候选路径，按优先级排序。"""
    candidates: list[Path] = []

    hermes_config = os.getenv("HERMES_CONFIG")
    if hermes_config:
        candidates.append(Path(hermes_config).expanduser())

    hermes_home = os.getenv("HERMES_HOME")
    if hermes_home:
        candidates.append(Path(hermes_home).expanduser() / "config.yaml")
    return candidates


def _load_config_sync() -> dict[str, Any]:
    """同步读取运行时配置。"""
    for config_path in _runtime_config_candidates():
        if not config_path.exists():
            continue
        with config_path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        return data if isinstance(data, dict) else {}
    return {}


async def _skills_root() -> Path:
    """返回可配置的 skills 根目录。"""
    config = await asyncio.to_thread(_load_config_sync)
    configured = config.get("skills", {}).get("sre_root") if isinstance(config.get("skills"), dict) else None
    if isinstance(configured, str) and configured.strip():
        return Path(configured).expanduser().resolve()
    return (_project_root() / "skills" / "sre").resolve()


def _validate_name(value: str, field_name: str) -> None:
    """校验路径片段，拒绝路径穿越。"""
    if not value or ".." in value or "/" in value or "\\" in value:
        raise ValueError(f"非法的 {field_name}")


async def list_drafts() -> list[dict[str, Any]]:
    """列出所有草稿。"""
    skills_root = await _skills_root()
    drafts_root = skills_root / "drafts"
    if not drafts_root.exists():
        return []

    def _list() -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for draft_path in sorted(drafts_root.glob("*/SKILL.md")):
            lines = draft_path.read_text(encoding="utf-8").splitlines()
            summary_line = next((line.strip() for line in lines if line.strip() and not line.startswith("#")), "")
            results.append(
                {
                    "incident_id": draft_path.parent.name,
                    "path": str(draft_path),
                    "summary_line": summary_line,
                }
            )
        return results

    return await asyncio.to_thread(_list)


async def promote_draft(incident_id: str, target_name: str) -> dict[str, Any]:
    """将草稿移动到 runbooks 目录。"""
    _validate_name(incident_id, "incident_id")
    _validate_name(target_name, "target_name")
    skills_root = await _skills_root()
    draft_dir = skills_root / "drafts" / incident_id
    draft_path = draft_dir / "SKILL.md"
    target_dir = skills_root / "runbooks" / target_name
    target_path = target_dir / "SKILL.md"

    def _promote() -> dict[str, Any]:
        if not draft_path.exists():
            return {"ok": False, "message": f"草稿不存在: {incident_id}"}
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(draft_path), str(target_path))
        shutil.rmtree(draft_dir)
        return {"ok": True, "incident_id": incident_id, "path": str(target_path)}

    return await asyncio.to_thread(_promote)


async def discard_draft(incident_id: str) -> dict[str, Any]:
    """删除指定草稿目录。"""
    _validate_name(incident_id, "incident_id")
    skills_root = await _skills_root()
    draft_dir = skills_root / "drafts" / incident_id

    def _discard() -> dict[str, Any]:
        if not draft_dir.exists():
            return {"ok": False, "message": f"草稿不存在: {incident_id}"}
        shutil.rmtree(draft_dir)
        return {"ok": True, "incident_id": incident_id}

    return await asyncio.to_thread(_discard)


SKILL_LIST_DRAFTS_SCHEMA = {
    "name": "skill_list_drafts",
    "description": "列出当前所有待审核 Skill 草稿。",
    "parameters": {"type": "object", "properties": {}},
}

SKILL_PROMOTE_DRAFT_SCHEMA = {
    "name": "skill_promote_draft",
    "description": "将指定草稿上线到 runbooks 目录。",
    "parameters": {
        "type": "object",
        "properties": {
            "incident_id": {"type": "string", "description": "草稿事件 ID"},
            "target_name": {"type": "string", "description": "上线目录名"},
        },
        "required": ["incident_id", "target_name"],
    },
}

SKILL_DISCARD_DRAFT_SCHEMA = {
    "name": "skill_discard_draft",
    "description": "丢弃指定 Skill 草稿。",
    "parameters": {
        "type": "object",
        "properties": {
            "incident_id": {"type": "string", "description": "草稿事件 ID"},
        },
        "required": ["incident_id"],
    },
}


async def _tool_skill_list_drafts(args: dict[str, Any], **_: Any) -> str:
    """工具入口：列出草稿。"""
    del args
    return json.dumps(await list_drafts(), ensure_ascii=False)


async def _tool_skill_promote_draft(args: dict[str, Any], **_: Any) -> str:
    """工具入口：上线草稿。"""
    return json.dumps(await promote_draft(args.get("incident_id", ""), args.get("target_name", "")), ensure_ascii=False)


async def _tool_skill_discard_draft(args: dict[str, Any], **_: Any) -> str:
    """工具入口：删除草稿。"""
    return json.dumps(await discard_draft(args.get("incident_id", "")), ensure_ascii=False)


registry.register(name="skill_list_drafts", toolset="sre", schema=SKILL_LIST_DRAFTS_SCHEMA, handler=_tool_skill_list_drafts, is_async=True)
registry.register(name="skill_promote_draft", toolset="sre", schema=SKILL_PROMOTE_DRAFT_SCHEMA, handler=_tool_skill_promote_draft, is_async=True)
registry.register(name="skill_discard_draft", toolset="sre", schema=SKILL_DISCARD_DRAFT_SCHEMA, handler=_tool_skill_discard_draft, is_async=True)
