"""Kubernetes 命令安全校验层。"""

from __future__ import annotations

import shlex
from typing import Dict, List, Optional


READ_SUBCOMMANDS = {
    "get",
    "describe",
    "logs",
    "top",
    "api-resources",
    "explain",
    "diff",
}

WRITE_SUBCOMMANDS = {
    "scale",
    "patch",
    "apply",
    "create",
    "edit",
    "rollout",
    "cordon",
    "drain",
    "taint",
    "label",
    "annotate",
}

EXEC_SUBCOMMANDS = {
    "exec",
    "cp",
    "port-forward",
    "run",
    "attach",
    "debug",
}

FORBIDDEN_SUBCOMMANDS = {"proxy"}

DELETE_WRITE_DANGEROUS_RESOURCES = {
    "pod",
    "pods",
    "deployment",
    "deployments",
    "service",
    "services",
    "configmap",
    "configmaps",
}

DELETE_EXEC_RESOURCES = {
    "namespace",
    "namespaces",
    "node",
    "nodes",
    "pv",
    "pvs",
    "persistentvolume",
    "persistentvolumes",
    "crd",
    "crds",
    "customresourcedefinition",
    "customresourcedefinitions",
}

GLOBAL_FLAGS_WITH_VALUE = {
    "-n",
    "--namespace",
    "-A",
    "--all-namespaces",
    "--context",
    "--cluster",
    "--user",
    "--server",
    "--request-timeout",
    "-f",
    "--filename",
    "-o",
    "--output",
    "-l",
    "--selector",
    "--field-selector",
    "--kubeconfig",
    "--as",
    "--as-group",
    "--token",
    "--certificate-authority",
    "--client-certificate",
    "--client-key",
    "--cache-dir",
}

LEVEL_TO_TOOL = {
    "read": "k8s_read",
    "write": "k8s_write",
    "write_dangerous": "k8s_write",
    "exec": "k8s_exec",
    "forbidden": "none",
}


def _build_result(level: str, subcommand: str, resource: str, reason: str) -> Dict[str, str]:
    """构造统一分类结果。"""
    return {
        "level": level,
        "subcommand": subcommand,
        "resource": resource,
        "reason": reason,
    }


def _safe_split(command: str) -> List[str]:
    """按 shell 规则拆分命令，失败时退化为空。"""
    try:
        return shlex.split(command)
    except ValueError:
        return []


def _strip_kubectl_prefix(tokens: List[str]) -> List[str]:
    """移除 env、sudo 等前缀，定位到 kubectl 主命令。"""
    idx = 0
    while idx < len(tokens):
        token = tokens[idx]
        if token == "env":
            idx += 1
            while idx < len(tokens) and "=" in tokens[idx] and not tokens[idx].startswith("-"):
                idx += 1
            continue
        if token == "sudo":
            idx += 1
            while idx < len(tokens) and tokens[idx].startswith("-"):
                idx += 1
            continue
        if token in {"kubectl", "oc"}:
            return tokens[idx:]
        idx += 1
    return []


def _next_non_flag(tokens: List[str], start: int) -> Optional[str]:
    """提取从指定位置开始的下一个资源标识。"""
    idx = start
    while idx < len(tokens):
        token = tokens[idx]
        if token == "--":
            return ""
        if token in GLOBAL_FLAGS_WITH_VALUE:
            idx += 2
            continue
        if token.startswith("--"):
            if "=" in token:
                idx += 1
                continue
            idx += 1
            continue
        if token.startswith("-") and token != "-":
            idx += 1
            continue
        return token
    return ""


def _normalize_resource(resource: str) -> str:
    """提取资源主类型，去掉名称或 group 后缀。"""
    if not resource:
        return ""
    first = resource.split(",", 1)[0]
    first = first.split("/", 1)[0]
    return first.lower()


def _extract_subcommand_and_resource(tokens: List[str]) -> tuple[str, str]:
    """从 kubectl token 中提取子命令和资源。"""
    idx = 1
    while idx < len(tokens) and tokens[idx].startswith("-"):
        if tokens[idx] in GLOBAL_FLAGS_WITH_VALUE and idx + 1 < len(tokens):
            idx += 2
            continue
        idx += 1

    if idx >= len(tokens):
        return "", ""

    subcommand = tokens[idx].lower()
    if subcommand == "auth":
        next_token = _next_non_flag(tokens, idx + 1)
        if next_token == "can-i":
            return "auth can-i", ""
        return "auth", next_token or ""

    resource = _next_non_flag(tokens, idx + 1) or ""
    return subcommand, _normalize_resource(resource)


async def classify_command(command: str) -> dict:
    """返回 Kubernetes 命令的安全级别分类。"""
    stripped = (command or "").strip()
    if not stripped:
        return _build_result("write", "", "", "命令为空，按写操作处理")

    tokens = _safe_split(stripped)
    if not tokens:
        return _build_result("write", "", "", "命令无法解析，按写操作处理")

    kubectl_tokens = _strip_kubectl_prefix(tokens)
    if not kubectl_tokens:
        return _build_result("write", "", "", "未检测到 kubectl 命令，按写操作处理")

    subcommand, resource = _extract_subcommand_and_resource(kubectl_tokens)
    if not subcommand:
        return _build_result("write", "", resource, "未识别到子命令，按写操作处理")

    if subcommand in FORBIDDEN_SUBCOMMANDS:
        return _build_result("forbidden", subcommand, resource, "proxy 会开放网络通道，已禁止执行")

    if subcommand in EXEC_SUBCOMMANDS:
        return _build_result("exec", subcommand, resource, f"{subcommand} 属于高风险执行类命令")

    if subcommand == "delete":
        if resource in DELETE_EXEC_RESOURCES:
            return _build_result("exec", subcommand, resource, f"delete {resource} 属于高危执行级别")
        if resource in DELETE_WRITE_DANGEROUS_RESOURCES:
            return _build_result("write_dangerous", subcommand, resource, f"delete {resource} 需要标准审批和二次确认")
        return _build_result("write", subcommand, resource, "delete 默认按写操作处理")

    if subcommand in READ_SUBCOMMANDS or subcommand == "auth can-i":
        return _build_result("read", subcommand, resource, f"{subcommand} 在只读白名单内")

    if subcommand in WRITE_SUBCOMMANDS:
        return _build_result("write", subcommand, resource, f"{subcommand} 属于写操作命令")

    return _build_result("write", subcommand, resource, "未知子命令，默认按写操作处理")


def _expected_level_for_tool(tool_level: str) -> str:
    """将工具名或级别归一化为目标级别。"""
    normalized = (tool_level or "").strip().lower()
    mapping = {
        "k8s_read": "read",
        "read": "read",
        "k8s_write": "write",
        "write": "write",
        "write_dangerous": "write",
        "k8s_exec": "exec",
        "exec": "exec",
    }
    return mapping.get(normalized, normalized)


async def guard_check(command: str, tool_level: str) -> dict:
    """检查 kubectl 命令是否匹配当前工具级别。"""
    classification = await classify_command(command)
    level = classification["level"]
    expected = _expected_level_for_tool(tool_level)

    if level == "forbidden":
        return {
            "allowed": False,
            "message": f"命令被禁止：{classification['reason']}",
            "classification": classification,
        }

    if expected == "read":
        if level == "read":
            return {
                "allowed": True,
                "message": "命令与 k8s_read 匹配",
                "classification": classification,
            }
        target_tool = LEVEL_TO_TOOL.get(level, "k8s_write")
        return {
            "allowed": False,
            "message": f"mismatch: 当前工具是 k8s_read，但命令被识别为 {level}。请改用 {target_tool}",
            "classification": classification,
        }

    if expected == "write":
        if level in {"write", "write_dangerous"}:
            suffix = "；该命令还需要标准审批和二次确认" if level == "write_dangerous" else ""
            return {
                "allowed": True,
                "message": f"命令与 k8s_write 匹配{suffix}",
                "classification": classification,
            }
        target_tool = LEVEL_TO_TOOL.get(level, "k8s_exec")
        return {
            "allowed": False,
            "message": f"mismatch: 当前工具是 k8s_write，但命令被识别为 {level}。请改用 {target_tool}",
            "classification": classification,
        }

    if expected == "exec":
        if level == "exec":
            return {
                "allowed": True,
                "message": "命令与 k8s_exec 匹配",
                "classification": classification,
            }
        target_tool = LEVEL_TO_TOOL.get(level, "k8s_write")
        return {
            "allowed": False,
            "message": f"mismatch: 当前工具是 k8s_exec，但命令被识别为 {level}。请改用 {target_tool}",
            "classification": classification,
        }

    return {
        "allowed": False,
        "message": f"未知工具级别: {tool_level}",
        "classification": classification,
    }
