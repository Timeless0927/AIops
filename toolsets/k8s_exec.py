"""Kubernetes 高危执行工具入口。"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

from k8s_guard import guard_check
from k8s_read import _run_kubectl, check_k8s_requirements
from k8s_redact import redact_k8s_output
from sre_extractor import extract_if_needed
from tools.registry import registry


K8S_EXEC_SCHEMA = {
    "name": "k8s_exec",
    "description": "提交 Kubernetes 高危执行类命令，返回高级审批请求信息。",
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的 kubectl exec/cp/port-forward 等高危命令",
            },
            "context": {
                "type": "string",
                "description": "可选的 kube context，会映射为 --context 参数",
            },
        },
        "required": ["command"],
    },
}


async def k8s_exec(command: str, context: str | None = None) -> Dict[str, Any]:
    """校验高危 Kubernetes 命令并返回审批请求。"""
    guard_result = await guard_check(command, "exec")
    if not guard_result["allowed"]:
        return {
            "ok": False,
            "error": guard_result["message"],
            "classification": guard_result["classification"],
        }

    return {
        "ok": True,
        "requires_approval": True,
        "approval_level": "elevated",
        "requires_can_approve": True,
        "command": command,
        "context": context,
        "classification": guard_result["classification"],
    }


async def execute_approved(command: str, context: str | None = None) -> Dict[str, Any]:
    """执行已通过高级审批的高危命令。"""
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
    name="k8s_exec",
    toolset="k8s",
    schema=K8S_EXEC_SCHEMA,
    handler=lambda args, **kw: json.dumps(
        asyncio.run(k8s_exec(args.get("command", ""), args.get("context"))),
        ensure_ascii=False,
    ),
    check_fn=check_k8s_requirements,
    is_async=False,
    emoji="☸️",
    max_result_size_chars=100_000,
)
