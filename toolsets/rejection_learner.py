"""从审批拒绝中学习经验。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict


def _load_tool_module(module_filename: str, module_name: str):
    """按文件路径加载工具模块。"""
    if module_name in sys.modules:
        return sys.modules[module_name]

    module_path = Path(__file__).resolve().parent / module_filename
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块: {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _ensure_registry_import() -> None:
    """确保可以导入 Hermes 的工具注册器。"""
    hermes_root = Path(__file__).resolve().parents[1] / "hermes-agent"
    if str(hermes_root) not in sys.path:
        sys.path.insert(0, str(hermes_root))


_ensure_registry_import()

from tools.registry import registry  # noqa: E402


approval_async = _load_tool_module("approval_async.py", "toolsets.approval_async.local")

_FILE_LOCK = threading.Lock()


def _project_root() -> Path:
    """返回项目根目录。"""
    return Path(__file__).resolve().parents[1]


def _lessons_path() -> Path:
    """返回拒绝经验文件路径。"""
    env_dir = os.getenv("AIOPS_DATA_DIR")
    base_dir = Path(env_dir).expanduser() if env_dir else _project_root() / "data"
    path = base_dir / "rejection_lessons.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _read_lessons_sync() -> list[dict[str, Any]]:
    """同步读取经验列表。"""
    path = _lessons_path()
    if not path.exists():
        return []

    with _FILE_LOCK:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    return data if isinstance(data, list) else []


def _write_lessons_sync(lessons: list[dict[str, Any]]) -> None:
    """同步写入经验列表。"""
    path = _lessons_path()
    with _FILE_LOCK:
        with path.open("w", encoding="utf-8") as handle:
            json.dump(lessons, handle, ensure_ascii=False, indent=2)


def _append_lesson_sync(entry: dict[str, Any]) -> int:
    """原子性追加一条经验记录并返回 lesson_id。"""
    with _FILE_LOCK:
        path = _lessons_path()
        lessons: list[dict[str, Any]] = []
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, list):
                lessons = data

        lesson_id = (int(lessons[-1].get("id", 0)) + 1) if lessons else 1
        entry["id"] = lesson_id
        lessons.append(entry)

        with path.open("w", encoding="utf-8") as handle:
            json.dump(lessons, handle, ensure_ascii=False, indent=2)

    return lesson_id


async def record_rejection_lesson(approval_id: str, reason: str, context: dict) -> dict:
    """将拒绝原因记录为可复用经验。"""
    context = context if isinstance(context, dict) else {}
    approval = await approval_async.check_approval(approval_id)

    operation_type = str(context.get("operation_type") or approval.get("operation_type") or "unknown").strip()
    namespace = str(context.get("namespace") or approval.get("namespace") or "unknown").strip()
    lesson_text = f"在 {namespace} 环境下不应该 {operation_type}，因为 {reason}"

    entry = {
        "timestamp": time.time(),
        "approval_id": approval_id,
        "operation_type": operation_type,
        "namespace": namespace,
        "lesson": lesson_text,
    }
    lesson_id = await asyncio.to_thread(_append_lesson_sync, entry)
    return {"lesson_id": lesson_id, "lesson_text": lesson_text}


async def get_rejection_stats(days: int = 30) -> dict:
    """统计最近一段时间的审批拒绝率。"""
    since = time.time() - max(1, int(days)) * 86400

    def _read_stats() -> dict:
        with approval_async._DB._lock:
            rows = approval_async._DB._conn.execute(
                """
                SELECT operation_type, status, COUNT(*) AS count
                FROM approvals
                WHERE created_at >= ?
                GROUP BY operation_type, status
                """,
                (since,),
            ).fetchall()

        grouped: dict[str, dict[str, int]] = {}
        for row in rows:
            operation_type = str(row["operation_type"])
            grouped.setdefault(operation_type, {"total": 0, "denied": 0})
            grouped[operation_type]["total"] += int(row["count"])
            if str(row["status"]) == "denied":
                grouped[operation_type]["denied"] += int(row["count"])

        stats = []
        high_rejection_types = []
        for operation_type, values in grouped.items():
            total = values["total"]
            denied = values["denied"]
            ratio = (denied / total) if total else 0.0
            stats.append(
                {
                    "operation_type": operation_type,
                    "total": total,
                    "denied": denied,
                    "ratio": ratio,
                }
            )
            if ratio > 0.3:
                high_rejection_types.append(operation_type)

        stats.sort(key=lambda item: item["operation_type"])
        return {"stats": stats, "high_rejection_types": high_rejection_types}

    return await asyncio.to_thread(_read_stats)


async def get_lessons(limit: int = 20) -> list[dict]:
    """读取最近的拒绝经验。"""
    lessons = await asyncio.to_thread(_read_lessons_sync)
    return lessons[-max(1, int(limit)) :] if lessons else []


SRE_RECORD_REJECTION_SCHEMA = {
    "name": "sre_record_rejection",
    "description": "记录一次审批拒绝教训。",
    "parameters": {
        "type": "object",
        "properties": {
            "approval_id": {"type": "string"},
            "reason": {"type": "string"},
            "context": {"type": "object"},
        },
        "required": ["approval_id", "reason", "context"],
    },
}

SRE_REJECTION_STATS_SCHEMA = {
    "name": "sre_rejection_stats",
    "description": "统计近期审批拒绝率。",
    "parameters": {
        "type": "object",
        "properties": {
            "days": {"type": "integer", "description": "统计周期，默认 30 天"},
        },
    },
}


async def _tool_record_rejection(args: Dict[str, Any], **_: Any) -> str:
    """工具入口：记录拒绝经验。"""
    result = await record_rejection_lesson(
        str(args.get("approval_id", "")),
        str(args.get("reason", "")),
        args.get("context") if isinstance(args.get("context"), dict) else {},
    )
    return json.dumps(result, ensure_ascii=False)


async def _tool_rejection_stats(args: Dict[str, Any], **_: Any) -> str:
    """工具入口：查询拒绝率统计。"""
    result = await get_rejection_stats(int(args.get("days", 30) or 30))
    return json.dumps(result, ensure_ascii=False)


registry.register(name="sre_record_rejection", toolset="sre", schema=SRE_RECORD_REJECTION_SCHEMA, handler=_tool_record_rejection, is_async=True)
registry.register(name="sre_rejection_stats", toolset="sre", schema=SRE_REJECTION_STATS_SCHEMA, handler=_tool_rejection_stats, is_async=True)
