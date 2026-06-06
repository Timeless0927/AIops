"""Kubernetes 只读工具入口。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

if __package__:
    from . import audit_log
    from .k8s_guard import classify_command, guard_check
    from .k8s_redact import redact_k8s_output
    from .permission_guard import check_tool_access
    from .sre_extractor import extract_if_needed
else:  # pragma: no cover - 兼容脚本式直接导入
    import audit_log  # type: ignore
    from k8s_guard import classify_command, guard_check
    from k8s_redact import redact_k8s_output
    from permission_guard import check_tool_access
    from sre_extractor import extract_if_needed

try:
    from tools.registry import registry
except ImportError:  # pragma: no cover - 本地测试未安装 hermes-agent 时使用
    class _NoopRegistry:
        def register(self, **_: Any) -> None:
            return None

    registry = _NoopRegistry()


READ_ENVELOPE_VERSION = "result.envelope.v1"
DEFAULT_TIMEOUT_SECONDS = 15
MAX_TIMEOUT_SECONDS = 30
DEFAULT_OUTPUT_LIMIT_BYTES = 256 * 1024
MAX_OUTPUT_LIMIT_BYTES = 1024 * 1024
PROD_LOG_DEFAULT_TAIL = 200
PROD_LOG_MAX_TAIL = 1000
PROD_LOG_DEFAULT_SINCE = "30m"
PROD_LOG_MAX_WINDOW_SECONDS = 2 * 60 * 60

FORBIDDEN_READ_SUBCOMMANDS = {
    "apply",
    "attach",
    "auth",
    "config",
    "cordon",
    "cp",
    "create",
    "debug",
    "delete",
    "drain",
    "edit",
    "exec",
    "label",
    "patch",
    "plugin",
    "port-forward",
    "proxy",
    "replace",
    "run",
    "scale",
    "set",
    "taint",
}

GET_RESOURCES = {
    "pod": "Pod",
    "pods": "Pod",
    "po": "Pod",
    "event": "Event",
    "events": "Event",
    "ev": "Event",
    "deployment": "Deployment",
    "deployments": "Deployment",
    "deploy": "Deployment",
    "statefulset": "StatefulSet",
    "statefulsets": "StatefulSet",
    "sts": "StatefulSet",
    "service": "Service",
    "services": "Service",
    "svc": "Service",
    "ingress": "Ingress",
    "ingresses": "Ingress",
    "ing": "Ingress",
    "configmap": "ConfigMap",
    "configmaps": "ConfigMap",
    "cm": "ConfigMap",
}

DESCRIBE_RESOURCES = {
    "pod": "Pod",
    "pods": "Pod",
    "po": "Pod",
    "deployment": "Deployment",
    "deployments": "Deployment",
    "deploy": "Deployment",
    "statefulset": "StatefulSet",
    "statefulsets": "StatefulSet",
    "sts": "StatefulSet",
    "service": "Service",
    "services": "Service",
    "svc": "Service",
    "ingress": "Ingress",
    "ingresses": "Ingress",
    "ing": "Ingress",
}

ROLLOUT_HISTORY_RESOURCES = {
    "deployment": "Deployment",
    "deployments": "Deployment",
    "deploy": "Deployment",
    "statefulset": "StatefulSet",
    "statefulsets": "StatefulSet",
    "sts": "StatefulSet",
}

READ_FLAGS_WITH_VALUE = {
    "-n",
    "--namespace",
    "-l",
    "--selector",
    "-o",
    "--output",
    "--field-selector",
}

LOG_FLAGS_WITH_VALUE = {
    "--since",
    "--since-time",
    "--tail",
    "--container",
    "-c",
}

LOG_BOOLEAN_FLAGS = {"--previous"}
OUTPUT_FORMATS = {"wide", "json", "yaml"}
ALL_READ_VALUE_FLAGS = READ_FLAGS_WITH_VALUE | LOG_FLAGS_WITH_VALUE
ALL_READ_BOOLEAN_FLAGS = LOG_BOOLEAN_FLAGS | {"-A", "--all-namespaces"}
FORBIDDEN_GLOBAL_FLAGS = {
    "--as",
    "--as-group",
    "--as-uid",
    "--cache-dir",
    "--certificate-authority",
    "--client-certificate",
    "--client-key",
    "--cluster",
    "--context",
    "--insecure-skip-tls-verify",
    "--kubeconfig",
    "--password",
    "--profile",
    "--profile-output",
    "--request-timeout",
    "--server",
    "--tls-server-name",
    "--token",
    "--user",
    "--username",
}


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

RUN_K8S_READ_SCHEMA = {
    "name": "run_k8s_read",
    "description": "通过 K8s Gateway 受控只读路径执行 kubectl argv，并返回 V1 ResultEnvelope。",
    "parameters": {
        "type": "object",
        "properties": {
            "cluster_id": {"type": "string", "description": "目标集群 ID"},
            "namespace": {"type": "string", "description": "目标命名空间"},
            "argv": {
                "type": "array",
                "items": {"type": "string"},
                "description": "kubectl argv 数组，argv[0] 必须为 kubectl，不接受 shell string",
            },
            "reason": {"type": "string", "description": "执行原因"},
            "operator_profile": {"type": "object", "description": "可选操作者权限资料"},
            "namespace_scope": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选 namespace 授权范围",
            },
            "allow_all_namespaces": {"type": "boolean"},
            "environment": {"type": "string", "description": "prod/production 时启用生产只读限制"},
            "timeout_seconds": {"type": "integer"},
            "output_limit_bytes": {"type": "integer"},
            "request_id": {"type": "string"},
            "correlation_id": {"type": "string"},
            "task_id": {"type": "string"},
            "command_id": {"type": "string"},
            "actor_id": {"type": "string"},
            "actor_type": {"type": "string"},
            "agent_id": {"type": "string"},
            "connector_id": {"type": "string"},
        },
        "required": ["cluster_id", "namespace", "argv", "reason"],
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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _argv_to_command(argv: List[str]) -> str:
    return " ".join(shlex.quote(token) for token in argv)


def _sha256_text(text: str | None) -> str:
    return "sha256:" + hashlib.sha256((text or "").encode("utf-8", errors="replace")).hexdigest()


def _sha256_json(payload: Any) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _ref_id(digest: str) -> str:
    return f"ev_k8s_{digest.removeprefix('sha256:')[:16]}"


def _selector_app(argv: List[str]) -> str | None:
    selector = _flag_value(argv, {"-l", "--selector"})
    if not selector:
        return None
    for part in selector.split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key.strip() in {"app", "app.kubernetes.io/name"} and value.strip():
            return value.strip()
    return None


def _build_evidence_refs(
    args: Dict[str, Any],
    policy: Dict[str, Any],
    audit_ref: str | None,
) -> list[Dict[str, Any]]:
    argv = policy.get("argv") if isinstance(policy.get("argv"), list) else args.get("argv", [])
    if not isinstance(argv, list):
        argv = []
    digest = _sha256_json(
        {
            "cluster_id": policy.get("cluster_id") or args.get("cluster_id"),
            "namespace": policy.get("namespace") or args.get("namespace"),
            "argv": argv,
            "audit_ref": audit_ref,
        }
    )
    resource_name = str(policy.get("resource_name") or "") or _selector_app(argv)
    return [
        {
            "ref_id": _ref_id(digest),
            "source": "k8s",
            "cluster_id": str(policy.get("cluster_id") or args.get("cluster_id") or ""),
            "namespace": str(policy.get("namespace") or args.get("namespace") or ""),
            "service": resource_name or None,
            "time_range": None,
            "query_digest": digest,
            "cursor": None,
            "audit_ref": audit_ref,
            "resource_kind": policy.get("resource_kind"),
            "resource_name": resource_name,
            "action": policy.get("action"),
        }
    ]


def _normalize_resource_token(token: str) -> tuple[str, str | None]:
    if "," in token:
        return "__multi_resource__", None
    resource = token.strip().lower()
    name: str | None = None
    if "/" in resource:
        resource, name = resource.split("/", 1)
    if "." in resource:
        resource = resource.split(".", 1)[0]
    return resource, name or None


def _flag_value(tokens: List[str], flag_names: set[str]) -> str | None:
    idx = 0
    while idx < len(tokens):
        token = tokens[idx]
        if "=" in token:
            flag, value = token.split("=", 1)
            if flag in flag_names:
                return value
        if token in flag_names and idx + 1 < len(tokens):
            return tokens[idx + 1]
        idx += 1
    return None


def _has_flag(tokens: List[str], flag_names: set[str]) -> bool:
    for token in tokens:
        if token in flag_names:
            return True
        if "=" in token and token.split("=", 1)[0] in flag_names:
            return True
    return False


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "prod", "production"}
    return False


def _is_prod_context(cluster_id: str, namespace: str, environment: str | None, prod: Any = None) -> bool:
    if _parse_bool(prod):
        return True
    env = (environment or "").strip().lower()
    cluster = (cluster_id or "").strip().lower()
    ns = (namespace or "").strip().lower()
    return env in {"prod", "production"} or "prod" in cluster or ns == "production"


def _duration_seconds(value: str) -> int | None:
    raw = (value or "").strip().lower()
    if not raw:
        return None
    suffix = raw[-1]
    number = raw[:-1] if suffix in {"s", "m", "h"} else raw
    try:
        parsed = float(number)
    except ValueError:
        return None
    if parsed < 0:
        return None
    multiplier = {"s": 1, "m": 60, "h": 3600}.get(suffix, 1)
    return int(parsed * multiplier)


def _since_time_window_seconds(value: str) -> int | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int((datetime.now(timezone.utc) - parsed).total_seconds())


def _extract_namespace(argv: List[str]) -> str | None:
    return _flag_value(argv, {"-n", "--namespace"})


def _all_namespaces_requested(argv: List[str]) -> bool:
    return _has_flag(argv, {"-A", "--all-namespaces"})


def _validate_flag_set(
    trailing: List[str],
    value_flags: set[str],
    boolean_flags: set[str] | None = None,
) -> tuple[bool, str | None]:
    boolean_flags = boolean_flags or set()
    idx = 0
    while idx < len(trailing):
        token = trailing[idx]
        if token == "--":
            return False, "-- 分隔符不在只读路径允许范围内"
        if not token.startswith("-"):
            idx += 1
            continue

        if "=" in token:
            flag, value = token.split("=", 1)
            if flag not in value_flags:
                return False, f"参数 {flag} 不在只读 allowlist 内"
            if not value:
                return False, f"参数 {flag} 缺少取值"
            idx += 1
            continue

        if token in value_flags:
            if idx + 1 >= len(trailing) or trailing[idx + 1].startswith("-"):
                return False, f"参数 {token} 缺少取值"
            idx += 2
            continue

        if token in boolean_flags:
            idx += 1
            continue

        return False, f"参数 {token} 不在只读 allowlist 内"
    return True, None


def _validate_full_argv_flags(argv: List[str]) -> tuple[bool, str | None]:
    idx = 2
    while idx < len(argv):
        token = argv[idx]
        if token == "--":
            return False, "-- 分隔符不在只读路径允许范围内"
        if not token.startswith("-"):
            idx += 1
            continue

        flag = token.split("=", 1)[0] if "=" in token else token
        if flag in FORBIDDEN_GLOBAL_FLAGS:
            return False, f"参数 {flag} 会改变集群/身份边界，已禁止"
        if flag in ALL_READ_BOOLEAN_FLAGS:
            idx += 1
            continue
        if flag in ALL_READ_VALUE_FLAGS:
            if "=" in token:
                if not token.split("=", 1)[1]:
                    return False, f"参数 {flag} 缺少取值"
                idx += 1
                continue
            if idx + 1 >= len(argv) or argv[idx + 1].startswith("-"):
                return False, f"参数 {flag} 缺少取值"
            idx += 2
            continue
        return False, f"参数 {flag} 不在只读 allowlist 内"
    return True, None


def _extract_resource_and_name(tokens: List[str], start: int) -> tuple[str | None, str | None, int]:
    idx = start
    while idx < len(tokens):
        token = tokens[idx]
        if token.startswith("-"):
            flag = token.split("=", 1)[0] if "=" in token else token
            if flag in ALL_READ_VALUE_FLAGS and "=" not in token and idx + 1 < len(tokens):
                idx += 2
            else:
                idx += 1
            continue
        resource, inline_name = _normalize_resource_token(token)
        name = inline_name
        next_idx = idx + 1
        if inline_name is None and next_idx < len(tokens) and not tokens[next_idx].startswith("-"):
            name = tokens[next_idx]
            next_idx += 1
        return resource, name, next_idx
    return None, None, idx


def _validate_output_format(argv: List[str]) -> tuple[bool, str | None, str | None]:
    output = _flag_value(argv, {"-o", "--output"})
    if output is None:
        return True, None, None
    normalized = output.strip().lower()
    if normalized not in OUTPUT_FORMATS:
        return False, f"输出格式 {output} 不在只读 allowlist 内", normalized
    return True, None, normalized


def _validate_prod_logs(argv: List[str]) -> tuple[bool, str | None, List[str]]:
    normalized = list(argv)
    tail = _flag_value(normalized, {"--tail"})
    if tail is None:
        normalized.extend(["--tail", str(PROD_LOG_DEFAULT_TAIL)])
    else:
        try:
            tail_count = int(tail)
        except ValueError:
            return False, "--tail 必须是整数", normalized
        if tail_count < 0 or tail_count > PROD_LOG_MAX_TAIL:
            return False, f"prod logs --tail 不能超过 {PROD_LOG_MAX_TAIL}", normalized

    since = _flag_value(normalized, {"--since"})
    since_time = _flag_value(normalized, {"--since-time"})
    if since is None and since_time is None:
        normalized.extend(["--since", PROD_LOG_DEFAULT_SINCE])
    elif since is not None:
        seconds = _duration_seconds(since)
        if seconds is None:
            return False, "--since 必须是正向秒/分钟/小时窗口", normalized
        if seconds > PROD_LOG_MAX_WINDOW_SECONDS:
            return False, "prod logs 时间窗口不能超过 2h", normalized
    elif since_time is not None:
        seconds = _since_time_window_seconds(since_time)
        if seconds is None:
            return False, "--since-time 必须是合法 RFC3339 时间", normalized
        if seconds > PROD_LOG_MAX_WINDOW_SECONDS:
            return False, "prod logs --since-time 不能早于 2h", normalized

    return True, None, normalized


def _normalize_namespace_args(argv: List[str], namespace: str, allow_all_namespaces: bool) -> List[str]:
    if allow_all_namespaces or _all_namespaces_requested(argv) or _extract_namespace(argv):
        return list(argv)
    return [*argv, "-n", namespace]


async def _validate_controlled_read_args(args: Dict[str, Any]) -> Dict[str, Any]:
    cluster_id = str(args.get("cluster_id") or "").strip()
    namespace = str(args.get("namespace") or "").strip()
    argv = args.get("argv")
    reason = str(args.get("reason") or "").strip()
    environment = str(args.get("environment") or "").strip() or None
    is_prod = _is_prod_context(cluster_id, namespace, environment, args.get("prod"))

    if not cluster_id:
        return {"allowed": False, "code": "command_rejected", "message": "cluster_id 不能为空"}
    if not namespace:
        return {"allowed": False, "code": "namespace_out_of_scope", "message": "namespace 不能为空"}
    if not reason:
        return {"allowed": False, "code": "command_rejected", "message": "reason 不能为空"}
    if isinstance(argv, str):
        return {"allowed": False, "code": "command_rejected", "message": "argv 必须是数组，不允许 shell string"}
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
        return {"allowed": False, "code": "command_rejected", "message": "argv 必须是非空字符串数组"}
    if argv[0] != "kubectl":
        return {"allowed": False, "code": "command_rejected", "message": "argv[0] 必须是 kubectl"}
    if any(token in {";", "&&", "||", "|", "$(", "`"} for token in argv):
        return {"allowed": False, "code": "command_rejected", "message": "argv 不能包含 shell 控制符"}
    if len(argv) < 2:
        return {"allowed": False, "code": "command_rejected", "message": "缺少 kubectl 子命令"}
    ok, message = _validate_full_argv_flags(argv)
    if not ok:
        return {"allowed": False, "code": "command_rejected", "message": message}

    try:
        timeout_seconds = int(args.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        return {"allowed": False, "code": "command_rejected", "message": "timeout_seconds 必须是整数"}
    if timeout_seconds <= 0 or timeout_seconds > MAX_TIMEOUT_SECONDS:
        return {
            "allowed": False,
            "code": "command_rejected",
            "message": f"timeout_seconds 必须在 1 到 {MAX_TIMEOUT_SECONDS} 秒之间",
        }

    try:
        output_limit_bytes = int(args.get("output_limit_bytes") or DEFAULT_OUTPUT_LIMIT_BYTES)
    except (TypeError, ValueError):
        return {"allowed": False, "code": "command_rejected", "message": "output_limit_bytes 必须是整数"}
    if output_limit_bytes <= 0 or output_limit_bytes > MAX_OUTPUT_LIMIT_BYTES:
        return {
            "allowed": False,
            "code": "command_rejected",
            "message": f"output_limit_bytes 必须在 1 到 {MAX_OUTPUT_LIMIT_BYTES} 字节之间",
        }

    namespace_scope = args.get("namespace_scope")
    if isinstance(namespace_scope, list):
        scope = {str(item).strip() for item in namespace_scope if str(item).strip()}
        if scope and "*" not in scope and namespace not in scope:
            return {"allowed": False, "code": "namespace_out_of_scope", "message": f"namespace {namespace} 不在授权范围内"}

    operator_profile = args.get("operator_profile")
    if isinstance(operator_profile, dict):
        access = check_tool_access(operator_profile, "k8s_read", namespace)
        if not access.get("allowed"):
            access = check_tool_access(operator_profile, "run_k8s_read", namespace)
        if not access.get("allowed"):
            return {"allowed": False, "code": "permission_denied", "message": str(access.get("message") or "无权访问")}

    if _has_flag(argv, {"--raw"}):
        return {"allowed": False, "code": "command_rejected", "message": "--raw 在只读路径中禁止"}

    all_namespaces = _all_namespaces_requested(argv)
    raw_allow_all_namespaces = args.get("allow_all_namespaces", False)
    if raw_allow_all_namespaces is not None and not isinstance(raw_allow_all_namespaces, bool):
        return {"allowed": False, "code": "command_rejected", "message": "allow_all_namespaces 必须是 boolean"}
    allow_all_namespaces = raw_allow_all_namespaces is True
    if all_namespaces:
        all_namespace_scope_allowed = (
            isinstance(namespace_scope, list)
            and "*" in {str(item).strip() for item in namespace_scope}
        )
        if is_prod or not allow_all_namespaces:
            return {"allowed": False, "code": "namespace_out_of_scope", "message": "未授权 --all-namespaces"}
        if not all_namespace_scope_allowed:
            return {"allowed": False, "code": "namespace_out_of_scope", "message": "--all-namespaces 需要 namespace_scope=*"}

    argv_namespace = _extract_namespace(argv)
    if argv_namespace and argv_namespace != namespace:
        return {
            "allowed": False,
            "code": "namespace_out_of_scope",
            "message": f"argv namespace {argv_namespace} 与请求 namespace {namespace} 不一致",
        }

    command = _argv_to_command(argv)
    subcommand = argv[1].lower()
    classification = await classify_command(command)

    if subcommand in FORBIDDEN_READ_SUBCOMMANDS:
        return {"allowed": False, "code": "command_rejected", "message": f"kubectl {subcommand} 在 run_k8s_read 中禁止"}

    normalized_argv = _normalize_namespace_args(argv, namespace, all_namespaces)
    resource_kind = ""
    resource_name = ""
    action = f"kubectl_{subcommand}"

    if subcommand == "get":
        resource, name, trailing_idx = _extract_resource_and_name(normalized_argv, 2)
        if resource not in GET_RESOURCES:
            if resource in {"secret", "secrets"}:
                return {"allowed": False, "code": "command_rejected", "message": "读取 Secret 明文在 V1 中禁止"}
            return {"allowed": False, "code": "command_rejected", "message": f"get {resource or ''} 不在 read allowlist 内"}
        ok, message = _validate_flag_set(
            normalized_argv[trailing_idx:],
            READ_FLAGS_WITH_VALUE,
            {"-A", "--all-namespaces"},
        )
        if not ok:
            return {"allowed": False, "code": "command_rejected", "message": message}
        ok, message, output_format = _validate_output_format(normalized_argv)
        if not ok:
            return {"allowed": False, "code": "command_rejected", "message": message}
        resource_kind = GET_RESOURCES[resource]
        resource_name = name or ""
        action = f"get_{resource_kind.lower()}"
        if resource_kind == "ConfigMap" and is_prod and output_format in {"json", "yaml"}:
            return {"allowed": False, "code": "command_rejected", "message": "prod ConfigMap 不允许默认返回 json/yaml value"}

    elif subcommand == "describe":
        resource, name, trailing_idx = _extract_resource_and_name(normalized_argv, 2)
        if resource not in DESCRIBE_RESOURCES:
            return {"allowed": False, "code": "command_rejected", "message": f"describe {resource or ''} 不在 read allowlist 内"}
        ok, message = _validate_flag_set(
            normalized_argv[trailing_idx:],
            {"-n", "--namespace", "-l", "--selector", "--field-selector"},
        )
        if not ok:
            return {"allowed": False, "code": "command_rejected", "message": message}
        resource_kind = DESCRIBE_RESOURCES[resource]
        resource_name = name or ""
        action = f"describe_{resource_kind.lower()}"

    elif subcommand == "logs":
        resource, name, trailing_idx = _extract_resource_and_name(normalized_argv, 2)
        if resource in {"secret", "secrets"}:
            return {"allowed": False, "code": "command_rejected", "message": "读取 Secret 明文在 V1 中禁止"}
        if not resource:
            return {"allowed": False, "code": "command_rejected", "message": "logs 必须指定 pod 或 pod/name"}
        if name is not None and resource not in {"pod", "pods", "po"}:
            return {"allowed": False, "code": "command_rejected", "message": "run_k8s_read logs 仅允许 pod 或 pod/name"}
        ok, message = _validate_flag_set(
            normalized_argv[trailing_idx:],
            {"-n", "--namespace", *LOG_FLAGS_WITH_VALUE},
            LOG_BOOLEAN_FLAGS,
        )
        if not ok:
            return {"allowed": False, "code": "command_rejected", "message": message}
        if is_prod:
            ok, message, normalized_argv = _validate_prod_logs(normalized_argv)
            if not ok:
                return {"allowed": False, "code": "command_rejected", "message": message}
        resource_kind = "Pod"
        resource_name = name or resource or ""
        action = "logs_pod"

    elif subcommand == "rollout":
        if len(normalized_argv) < 3 or normalized_argv[2].lower() != "history":
            return {"allowed": False, "code": "command_rejected", "message": "run_k8s_read 仅允许 rollout history"}
        resource, name, trailing_idx = _extract_resource_and_name(normalized_argv, 3)
        if resource not in ROLLOUT_HISTORY_RESOURCES:
            return {"allowed": False, "code": "command_rejected", "message": f"rollout history {resource or ''} 不在 read allowlist 内"}
        ok, message = _validate_flag_set(normalized_argv[trailing_idx:], {"-n", "--namespace"})
        if not ok:
            return {"allowed": False, "code": "command_rejected", "message": message}
        resource_kind = ROLLOUT_HISTORY_RESOURCES[resource]
        resource_name = name or ""
        action = "rollout_history"

    else:
        return {"allowed": False, "code": "command_rejected", "message": f"kubectl {subcommand} 不在 read allowlist 内"}

    return {
        "allowed": True,
        "cluster_id": cluster_id,
        "namespace": namespace,
        "argv": normalized_argv,
        "reason": reason,
        "classification": classification,
        "timeout_seconds": timeout_seconds,
        "output_limit_bytes": output_limit_bytes,
        "is_prod": is_prod,
        "resource_kind": resource_kind,
        "resource_name": resource_name,
        "action": action,
    }


def _truncate_bytes(value: bytes, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value.decode("utf-8", errors="replace"), False
    return value[:limit].decode("utf-8", errors="replace"), True


async def _run_kubectl_argv(argv: List[str], timeout_seconds: int, output_limit_bytes: int) -> Dict[str, Any]:
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        process.kill()
        stdout_bytes, stderr_bytes = await process.communicate()
        stdout, stdout_truncated = _truncate_bytes(stdout_bytes, output_limit_bytes)
        stderr, stderr_truncated = _truncate_bytes(stderr_bytes, output_limit_bytes)
        return {
            "ok": False,
            "exit_code": -1,
            "stdout": stdout,
            "stderr": stderr or f"kubectl 执行超时（{timeout_seconds}s）",
            "executed_command": argv,
            "truncated": stdout_truncated or stderr_truncated,
            "error_code": "timeout",
        }
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": f"kubectl 启动失败: {exc}",
            "executed_command": argv,
            "truncated": False,
            "error_code": "backend_unavailable",
        }

    stdout, stdout_truncated = _truncate_bytes(stdout_bytes, output_limit_bytes)
    stderr, stderr_truncated = _truncate_bytes(stderr_bytes, output_limit_bytes)
    return {
        "ok": process.returncode == 0,
        "exit_code": process.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "executed_command": argv,
        "truncated": stdout_truncated or stderr_truncated,
        "error_code": None if process.returncode == 0 else "execution_failed",
    }


def _build_result_envelope(
    args: Dict[str, Any],
    status: str,
    stdout: str = "",
    stderr: str = "",
    exit_code: int | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    truncated: bool = False,
    audit_ref: str | None = None,
    evidence_refs: list[Dict[str, Any]] | None = None,
    error: Dict[str, str] | None = None,
) -> Dict[str, Any]:
    result_ref = evidence_refs[0]["ref_id"] if evidence_refs else None
    return {
        "envelope_version": READ_ENVELOPE_VERSION,
        "task_id": args["task_id"],
        "command_id": args["command_id"],
        "connector_id": args.get("connector_id"),
        "cluster_id": args.get("cluster_id"),
        "status": status,
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": exit_code,
        "started_at": started_at,
        "finished_at": finished_at,
        "truncated": truncated,
        "chunks": [],
        "result_ref": result_ref,
        "audit_ref": audit_ref,
        "evidence_refs": evidence_refs or [],
        "error": error,
    }


async def _record_run_k8s_read_audit(
    args: Dict[str, Any],
    policy: Dict[str, Any],
    *,
    decision: str,
    stdout: str,
    stderr: str,
    exit_code: int | None,
    truncated: bool,
    started_at: str | None,
    finished_at: str,
    error: Dict[str, str] | None = None,
) -> str | None:
    argv = policy.get("argv") if policy.get("argv") is not None else args.get("argv", [])
    cluster_id = str(policy.get("cluster_id") or args.get("cluster_id") or "")
    namespace = str(policy.get("namespace") or args.get("namespace") or "")
    details = {
        "request_id": args.get("request_id"),
        "correlation_id": args.get("correlation_id"),
        "task_id": args["task_id"],
        "command_id": args["command_id"],
        "actor_type": args.get("actor_type", "agent"),
        "actor_id": args.get("actor_id", "unknown"),
        "agent_id": args.get("agent_id"),
        "brain_provider": args.get("brain_provider"),
        "cluster_id": cluster_id,
        "namespace": namespace,
        "resource_kind": policy.get("resource_kind"),
        "resource_name": policy.get("resource_name"),
        "action": policy.get("action", "run_k8s_read"),
        "risk_level": "low",
        "requires_approval": False,
        "approval_id": None,
        "approval_status": None,
        "grant_id": None,
        "connector_id": args.get("connector_id"),
        "decision": decision,
        "reason": args.get("reason"),
        "argv_digest": _sha256_json(argv),
        "stdout_digest": _sha256_text(stdout),
        "stderr_digest": _sha256_text(stderr),
        "exit_code": exit_code,
        "truncated": truncated,
        "rollback_required": False,
        "requested_at": args.get("requested_at"),
        "started_at": started_at,
        "finished_at": finished_at,
        "error_code": None if error is None else error.get("code"),
        "error_message": None if error is None else error.get("message"),
    }

    try:
        audit_id = await audit_log.record_audit(
            who=str(args.get("actor_id") or "unknown"),
            what=f"run_k8s_read {_argv_to_command(list(argv)) if isinstance(argv, list) else argv}",
            cluster=cluster_id,
            namespace=namespace,
            trigger=str(args.get("trigger") or "mcp"),
            tool_level="read",
            tool_name="run_k8s_read",
            result=json.dumps(details, ensure_ascii=False, sort_keys=True),
            incident_id=args.get("incident_id"),
        )
    except Exception:
        return None
    return str(audit_id)


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


async def run_k8s_read(**kwargs: Any) -> Dict[str, Any]:
    """执行 AIO-50 受控只读路径，返回 ResultEnvelope V1。"""
    args = dict(kwargs)
    args.setdefault("task_id", f"task-read-{uuid.uuid4().hex}")
    args.setdefault("command_id", f"cmd-read-{uuid.uuid4().hex}")
    args.setdefault("requested_at", _utc_now())
    args.setdefault("connector_id", None)

    policy = await _validate_controlled_read_args(args)
    if not policy.get("allowed"):
        finished_at = _utc_now()
        error = {
            "code": str(policy.get("code") or "command_rejected"),
            "message": str(policy.get("message") or "命令被拒绝"),
        }
        audit_ref = await _record_run_k8s_read_audit(
            args,
            policy,
            decision="rejected",
            stdout="",
            stderr=error["message"],
            exit_code=None,
            truncated=False,
            started_at=None,
            finished_at=finished_at,
            error=error,
        )
        return _build_result_envelope(
            args,
            "failed",
            stderr=error["message"],
            finished_at=finished_at,
            audit_ref=audit_ref,
            error=error,
        )

    started_at = _utc_now()
    execution = await _run_kubectl_argv(
        policy["argv"],
        int(policy["timeout_seconds"]),
        int(policy["output_limit_bytes"]),
    )
    finished_at = _utc_now()
    stdout = await redact_k8s_output(str(execution.get("stdout") or ""), _argv_to_command(policy["argv"]))
    stderr = await redact_k8s_output(str(execution.get("stderr") or ""), _argv_to_command(policy["argv"]))
    exit_code = int(execution.get("exit_code", -1))
    error_code = execution.get("error_code")
    error = None
    if error_code:
        error = {
            "code": str(error_code),
            "message": stderr or stdout or str(error_code),
        }
    status = "succeeded" if execution.get("ok") else "failed"
    audit_ref = await _record_run_k8s_read_audit(
        args,
        policy,
        decision="executed" if execution.get("ok") else "failed",
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        truncated=bool(execution.get("truncated")),
        started_at=started_at,
        finished_at=finished_at,
        error=error,
    )
    evidence_refs = _build_evidence_refs(args, policy, audit_ref) if execution.get("ok") else []
    return _build_result_envelope(
        args,
        status,
        stdout=stdout if execution.get("ok") else "",
        stderr=stderr if not execution.get("ok") else stderr,
        exit_code=exit_code,
        started_at=started_at,
        finished_at=finished_at,
        truncated=bool(execution.get("truncated")),
        audit_ref=audit_ref,
        evidence_refs=evidence_refs,
        error=error,
    )


async def _tool_run_k8s_read(args: Dict[str, Any], **_: Any) -> str:
    """MCP facade 入口。"""
    return json.dumps(await run_k8s_read(**args), ensure_ascii=False)


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

registry.register(
    name="run_k8s_read",
    toolset="k8s",
    schema=RUN_K8S_READ_SCHEMA,
    handler=_tool_run_k8s_read,
    check_fn=check_k8s_requirements,
    is_async=True,
    emoji="☸️",
    max_result_size_chars=100_000,
)
