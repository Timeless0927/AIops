"""Kubernetes 工具输出脱敏模块。"""

from __future__ import annotations

import json
import re

from agent.redact import redact_sensitive_text


REDACTED = "[REDACTED]"
SENSITIVE_ENV_SUFFIXES = ("_KEY", "_SECRET", "_TOKEN", "_PASSWORD")

ENV_ASSIGNMENT_RE = re.compile(
    r"\b([A-Z][A-Z0-9_]*(?:_KEY|_SECRET|_TOKEN|_PASSWORD))\s*=\s*([^\s\"']+|\"[^\"]*\"|'[^']*')"
)

SECRET_JSON_KEY_RE = re.compile(
    r'("(?:token|password|secret|api[_-]?key|access[_-]?token|refresh[_-]?token)"\s*:\s*")([^\"]+)(")',
    re.IGNORECASE,
)

SECRET_BASE64_RE = re.compile(
    r"\b(?:[A-Za-z0-9+/]{4}){4,}(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?\b"
)


def _is_secret_get_command(command: str) -> bool:
    """判断当前命令是否为获取 Secret 的输出。"""
    lowered = (command or "").lower()
    return "kubectl" in lowered and " get " in f" {lowered} " and " secret" in f" {lowered} "


def _redact_yaml_secret_data(output: str) -> str:
    """脱敏 YAML 中 data/stringData 字段值。"""
    lines = output.splitlines()
    result = []
    in_secret_block = False
    block_indent = 0

    for line in lines:
        stripped = line.lstrip()
        indent = len(line) - len(stripped)

        if stripped.startswith(("data:", "stringData:")):
            in_secret_block = True
            block_indent = indent
            result.append(line)
            continue

        if in_secret_block:
            if not stripped:
                result.append(line)
                continue
            if indent <= block_indent:
                in_secret_block = False
            elif ":" in stripped:
                key, _, _value = stripped.partition(":")
                result.append(f"{' ' * indent}{key}: {REDACTED}")
                continue
            else:
                result.append(f"{' ' * indent}{REDACTED}")
                continue

        result.append(line)

    return "\n".join(result)


def _redact_json_secret_data(output: str) -> str:
    """脱敏 JSON 中 data/stringData 字段值。"""
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return output

    def _walk(node):
        if isinstance(node, dict):
            updated = {}
            for key, value in node.items():
                if key in {"data", "stringData"} and isinstance(value, dict):
                    updated[key] = {sub_key: REDACTED for sub_key in value.keys()}
                else:
                    updated[key] = _walk(value)
            return updated
        if isinstance(node, list):
            return [_walk(item) for item in node]
        return node

    return json.dumps(_walk(payload), ensure_ascii=False, indent=2)


def _decode_base64_text(text: str) -> str:
    """尝试解码 base64 文本，失败时返回空字符串。"""
    import base64

    padded = text + "=" * ((4 - len(text) % 4) % 4)
    try:
        decoded = base64.b64decode(padded, validate=True)
    except Exception:
        return ""
    try:
        return decoded.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _redact_secret_base64_matches(output: str) -> str:
    """脱敏解码后包含敏感关键字的 base64 内容。"""
    keywords = ("token", "password", "secret", "apikey", "api_key", "bearer")

    def _replace(match: re.Match[str]) -> str:
        token = match.group(0)
        decoded = _decode_base64_text(token).lower()
        if any(keyword in decoded for keyword in keywords):
            return REDACTED
        return token

    return SECRET_BASE64_RE.sub(_replace, output)


def _redact_sensitive_env_assignments(output: str) -> str:
    """脱敏环境变量赋值中的敏感值。"""
    return ENV_ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}={REDACTED}", output)


def _redact_sensitive_json_fields(output: str) -> str:
    """脱敏普通 JSON 中带敏感名的字段。"""
    return SECRET_JSON_KEY_RE.sub(lambda m: f"{m.group(1)}{REDACTED}{m.group(3)}", output)


async def redact_k8s_output(output: str, command: str) -> str:
    """对 Kubernetes 工具输出做脱敏处理。"""
    if output is None:
        return output

    redacted = redact_sensitive_text(str(output))
    if _is_secret_get_command(command):
        redacted = _redact_json_secret_data(redacted)
        redacted = _redact_yaml_secret_data(redacted)

    redacted = _redact_sensitive_env_assignments(redacted)
    redacted = _redact_sensitive_json_fields(redacted)
    redacted = _redact_secret_base64_matches(redacted)
    return redacted
