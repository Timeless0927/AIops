"""规则引擎降级模块。"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

import yaml

try:
    from tools.registry import registry
except ImportError:  # pragma: no cover - 测试环境兼容
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hermes-agent"))
    from tools.registry import registry


def _config_path() -> Path:
    """返回配置文件路径。"""
    return Path(__file__).resolve().parents[1] / "config.yaml"


def _load_fallback_rules() -> list[dict[str, Any]]:
    """从配置中加载 fallback_rules。"""
    try:
        path = _config_path()
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
        rules = config.get("fallback_rules")
        return rules if isinstance(rules, list) else []
    except Exception:
        return []


def _apply_template(action: str, context: dict[str, Any]) -> str:
    """替换规则动作中的模板变量。"""
    replaced = action
    for key in ("pod", "namespace", "node", "cluster"):
        placeholder = f"{{{key}}}"
        if placeholder not in replaced:
            continue
        safe_value = shlex.quote(str(context.get(key, "")))
        replaced = replaced.replace(placeholder, safe_value)
    return replaced


async def match_fallback_rule(alert_name: str, context: dict[str, Any]) -> dict[str, Any] | None:
    """匹配告警对应的规则引擎动作。"""
    rules = _load_fallback_rules()
    matched: dict[str, Any] | None = None
    default_rule: dict[str, Any] | None = None

    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if rule.get("alert") == alert_name:
            matched = rule
            break
        if "default" in rule:
            default_rule = rule

    selected = matched or default_rule
    if not isinstance(selected, dict):
        return None

    action = _apply_template(str(selected.get("action", "")), context)
    deliver = selected.get("deliver")
    return {"alert": alert_name, "action": action, "deliver": deliver}


def format_degradation_notice(reason: str) -> str:
    """生成规则降级提示语。"""
    return f"⚠️ AI 诊断暂时不可用（{reason}），已切换到规则引擎模式。"


SRE_FALLBACK_MATCH_SCHEMA = {
    "name": "sre_fallback_match",
    "description": "根据告警名匹配规则引擎降级动作。",
    "parameters": {
        "type": "object",
        "properties": {
            "alert_name": {"type": "string"},
            "context": {"type": "object"},
        },
        "required": ["alert_name", "context"],
    },
}


async def _tool_fallback_match(args: dict[str, Any], **_: Any) -> str:
    """工具入口：匹配降级规则。"""
    result = await match_fallback_rule(args.get("alert_name", ""), args.get("context", {}))
    return json.dumps(result, ensure_ascii=False)


registry.register(name="sre_fallback_match", toolset="sre", schema=SRE_FALLBACK_MATCH_SCHEMA, handler=_tool_fallback_match, is_async=True)
