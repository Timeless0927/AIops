"""自监控健康检查 Hook。"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any

try:
    from tools.registry import registry
except ImportError:  # pragma: no cover - 测试环境兼容
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hermes-agent"))
    from tools.registry import registry


_start_time = time.time()


def _load_module(module_filename: str, module_name: str):
    """按文件路径加载模块。"""
    if module_name in sys.modules:
        return sys.modules[module_name]

    module_path = Path(__file__).resolve().parent.parent / "toolsets" / module_filename
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


incident_store = _load_module("incident_store.py", "toolsets.incident_store")
audit_log = _load_module("audit_log.py", "toolsets.audit_log")


async def check_health() -> dict[str, Any]:
    """检查关键数据文件和运行时状态。"""
    incident_db = getattr(getattr(incident_store, "_STORE", None), "db_path", None)
    audit_db = getattr(getattr(audit_log, "_DB", None), "db_path", None)

    incident_ok = bool(isinstance(incident_db, Path) and incident_db.exists())
    audit_ok = bool(isinstance(audit_db, Path) and audit_db.exists())

    return {
        "healthy": incident_ok and audit_ok,
        "checks": {
            "incident_store_db": incident_ok,
            "audit_log_db": audit_ok,
        },
        "uptime_seconds": time.time() - _start_time,
    }


async def handle(event_type: str, context: dict[str, Any]) -> dict[str, Any]:
    """处理 gateway 启动健康检查。"""
    del context
    if event_type == "gateway:startup":
        return await check_health()
    return {"handled": False}


SRE_HEALTH_CHECK_SCHEMA = {
    "name": "sre_health_check",
    "description": "执行 SRE 自监控健康检查。",
    "parameters": {"type": "object", "properties": {}},
}


async def _tool_health_check(args: dict[str, Any], **_: Any) -> str:
    """工具入口：执行健康检查。"""
    del args
    result = await check_health()
    return json.dumps(result, ensure_ascii=False)


registry.register(name="sre_health_check", toolset="sre", schema=SRE_HEALTH_CHECK_SCHEMA, handler=_tool_health_check, is_async=True)
