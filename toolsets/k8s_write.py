"""Kubernetes 写操作工具入口。"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

try:
    from .k8s_guard import guard_check
    from .k8s_read import _run_kubectl, check_k8s_requirements
    from .k8s_redact import redact_k8s_output
    from .sre_extractor import extract_if_needed
except ImportError:  # pragma: no cover - 兼容脚本式直接导入
    from k8s_guard import guard_check
    from k8s_read import _run_kubectl, check_k8s_requirements
    from k8s_redact import redact_k8s_output
    from sre_extractor import extract_if_needed

try:
    from tools.registry import registry
except ImportError:  # pragma: no cover - 本地测试未安装 hermes-agent 时使用
    class _NoopRegistry:
        def register(self, **_: Any) -> None:
            return None

    registry = _NoopRegistry()


K8S_WRITE_SCHEMA = {
    "name": "k8s_write",
    "description": "提交 Kubernetes 写操作，返回审批请求信息。",
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的 kubectl 写操作命令，例如 kubectl apply -f deploy.yaml",
            },
            "context": {
                "type": "string",
                "description": "可选的 kube context，会映射为 --context 参数",
            },
        },
        "required": ["command"],
    },
}


def _approval_level_from_classification(classification: Dict[str, Any]) -> str:
    """根据分类结果映射审批等级。"""
    if classification.get("level") == "write_dangerous":
        return "dangerous"
    return "standard"


async def k8s_write(command: str, context: str | None = None) -> Dict[str, Any]:
    """校验 Kubernetes 写命令并返回审批请求。"""
    guard_result = await guard_check(command, "write")
    if not guard_result["allowed"]:
        return {
            "ok": False,
            "error": guard_result["message"],
            "classification": guard_result["classification"],
        }

    approval_level = _approval_level_from_classification(guard_result["classification"])
    return {
        "ok": True,
        "requires_approval": True,
        "approval_level": approval_level,
        "command": command,
        "context": context,
        "classification": guard_result["classification"],
    }


async def execute_approved(command: str, context: str | None = None) -> Dict[str, Any]:
    """执行已通过审批的写操作命令。"""
    execution = await _run_kubectl(command, context)
    combined_output = execution["stdout"] if execution["ok"] else execution["stderr"] or execution["stdout"]
    redacted_output = await redact_k8s_output(combined_output, command)
    extracted = await extract_if_needed(redacted_output, "k8s")
    return {
        "ok": execution["ok"],
        "command": command,
        "context": context,
        "exit_code": execution["exit_code"],
        "stdout": redacted_output if execution["ok"] else "",
        "stderr": redacted_output if not execution["ok"] else execution["stderr"],
        "result": extracted,
    }


registry.register(
    name="k8s_write",
    toolset="k8s",
    schema=K8S_WRITE_SCHEMA,
    handler=lambda args, **kw: json.dumps(
        asyncio.run(k8s_write(args.get("command", ""), args.get("context"))),
        ensure_ascii=False,
    ),
    check_fn=check_k8s_requirements,
    is_async=False,
    emoji="☸️",
    max_result_size_chars=100_000,
)
