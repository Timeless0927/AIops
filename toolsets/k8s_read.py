"""Kubernetes 只读工具入口。"""

from __future__ import annotations

import asyncio
import json
import shlex
from typing import Any, Dict, List

try:
    from .k8s_guard import guard_check
    from .k8s_redact import redact_k8s_output
    from .sre_extractor import extract_if_needed
except ImportError:  # pragma: no cover - 兼容脚本式直接导入
    from k8s_guard import guard_check
    from k8s_redact import redact_k8s_output
    from sre_extractor import extract_if_needed
from tools.registry import registry


K8S_READ_SCHEMA = {
    "name": "k8s_read",
    "description": "执行只读 kubectl 命令，返回脱敏后的结果。",
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的 kubectl 只读命令，例如 kubectl get pods -n default",
            },
            "context": {
                "type": "string",
                "description": "可选的 kube context，会映射为 --context 参数",
            },
        },
        "required": ["command"],
    },
}


def check_k8s_requirements() -> bool:
    """当前阶段仅要求系统存在 kubectl。"""
    import shutil

    return shutil.which("kubectl") is not None


def _normalize_command_tokens(command: str, context: str | None) -> List[str]:
    """将命令标准化为 subprocess 可执行的 token。"""
    tokens = shlex.split(command)
    if not tokens:
        raise ValueError("命令为空")

    if tokens[0] != "kubectl":
        raise ValueError("仅允许执行 kubectl 命令")

    if context and "--context" not in tokens:
        tokens = [tokens[0], "--context", context, *tokens[1:]]

    return tokens


async def _run_kubectl(command: str, context: str | None = None) -> Dict[str, Any]:
    """执行 kubectl 命令并返回原始输出。"""
    tokens = _normalize_command_tokens(command, context)
    process = await asyncio.create_subprocess_exec(
        *tokens,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        return {
            "ok": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": "kubectl 执行超时（60s）",
            "executed_command": tokens,
        }

    return {
        "ok": process.returncode == 0,
        "exit_code": process.returncode,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
        "executed_command": tokens,
    }


async def k8s_read(command: str, context: str | None = None) -> Dict[str, Any]:
    """执行只读 Kubernetes 命令。"""
    guard_result = await guard_check(command, "read")
    if not guard_result["allowed"]:
        return {
            "ok": False,
            "error": guard_result["message"],
            "classification": guard_result["classification"],
        }

    execution = await _run_kubectl(command, context)
    combined_output = execution["stdout"] if execution["ok"] else execution["stderr"] or execution["stdout"]
    redacted_output = await redact_k8s_output(combined_output, command)
    extracted = await extract_if_needed(redacted_output, "k8s")

    return {
        "ok": execution["ok"],
        "command": command,
        "context": context,
        "classification": guard_result["classification"],
        "exit_code": execution["exit_code"],
        "stdout": redacted_output if execution["ok"] else "",
        "stderr": redacted_output if not execution["ok"] else execution["stderr"],
        "result": extracted,
    }


registry.register(
    name="k8s_read",
    toolset="k8s",
    schema=K8S_READ_SCHEMA,
    handler=lambda args, **kw: json.dumps(
        asyncio.run(k8s_read(args.get("command", ""), args.get("context"))),
        ensure_ascii=False,
    ),
    check_fn=check_k8s_requirements,
    is_async=False,
    emoji="☸️",
    max_result_size_chars=100_000,
)
