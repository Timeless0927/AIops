"""Kubectl executor boundary for approved Connector command envelopes."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Sequence
from selectors import DefaultSelector, EVENT_READ
from typing import Any

from aiops.k8s import CommandEnvelope, ResultEnvelope
from toolsets.k8s_redact import redact_k8s_output


MAX_TIMEOUT_SECONDS = 600
MAX_OUTPUT_LIMIT_BYTES = 1024 * 1024
_MUTATING_SUBCOMMANDS = {
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
_GET_RESOURCES = {
    "pods",
    "pod",
    "po",
    "deployments",
    "deployment",
    "deploy",
    "events",
    "event",
    "ev",
    "services",
    "service",
    "svc",
    "configmaps",
    "configmap",
    "cm",
}
_DESCRIBE_RESOURCES = {"pods", "pod", "po", "deployments", "deployment", "deploy"}
_ROLLOUT_RESOURCES = {"deployments", "deployment", "deploy"}
_READ_FLAGS_WITH_VALUE = {"-n", "--namespace", "-l", "--selector", "-o", "--output", "--field-selector"}
_LOG_FLAGS_WITH_VALUE = {"-n", "--namespace", "--since", "--since-time", "--tail", "--container", "-c"}
_LOG_BOOLEAN_FLAGS = {"--previous"}
_ROLLOUT_FLAGS_WITH_VALUE = {"-n", "--namespace"}
_OUTPUT_FORMATS = {"wide", "json", "yaml"}
_LOW_RISK_LEVELS = {None, "", "low"}
_FORBIDDEN_GLOBAL_FLAGS = {
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
_ALWAYS_FORBIDDEN_FLAGS = {
    "-f",
    "--filename",
    "--watch",
    "--watch-only",
    "-w",
    "--recursive",
    "-R",
}
_SHELL_CONTROL_TOKENS = {";", "&&", "||", "|", "$(", "`"}


class _ParsedArgv:
    def __init__(self, resource: str | None, trailing: tuple[str, ...]) -> None:
        self.resource = resource
        self.trailing = trailing


def _argv_to_command(argv: Sequence[str]) -> str:
    return " ".join(argv)


def _result(
    envelope: CommandEnvelope,
    *,
    connector_id: str,
    status: str,
    stdout: str = "",
    stderr: str = "",
    exit_code: int | None = None,
    truncated: bool = False,
    error_code: str | None = None,
    error_message: str | None = None,
) -> ResultEnvelope:
    return ResultEnvelope(
        envelope_version="v1",
        task_id=envelope.task_id,
        command_id=envelope.command_id,
        connector_id=connector_id,
        cluster_id=envelope.cluster_id,
        status=status,
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        truncated=truncated,
        result_ref=f"k8s-read://{envelope.cluster_id}/{envelope.namespace}/{envelope.command_id}",
        error_code=error_code,
        error_message=error_message,
    )


def rejected_result(
    envelope: CommandEnvelope,
    *,
    connector_id: str,
    error_code: str,
    error_message: str,
) -> ResultEnvelope:
    return _result(
        envelope,
        connector_id=connector_id,
        status="command_rejected",
        stderr=error_message,
        error_code=error_code,
        error_message=error_message,
    )


def _resource_token(token: str) -> str:
    resource = token.strip().lower()
    if "," in resource:
        return "__multi_resource__"
    if "/" in resource:
        resource = resource.split("/", 1)[0]
    if "." in resource:
        resource = resource.split(".", 1)[0]
    return resource


def _flag_value(argv: Sequence[str], names: set[str]) -> str | None:
    index = 0
    while index < len(argv):
        token = argv[index]
        if "=" in token:
            flag, value = token.split("=", 1)
            if flag in names:
                return value
        if token in names and index + 1 < len(argv):
            return argv[index + 1]
        index += 1
    return None


def _flag_name(token: str) -> str:
    return token.split("=", 1)[0] if "=" in token else token


def _validate_complete_argv_flags(argv: Sequence[str], allowed_value_flags: set[str], allowed_boolean_flags: set[str]) -> None:
    index = 2
    while index < len(argv):
        token = argv[index]
        if token == "--":
            raise ValueError("command_rejected: -- separator is not allowed")
        if not token.startswith("-"):
            index += 1
            continue

        flag = _flag_name(token)
        if flag in _FORBIDDEN_GLOBAL_FLAGS:
            raise ValueError(f"command_rejected: {flag} changes cluster or identity boundary")
        if flag in _ALWAYS_FORBIDDEN_FLAGS:
            raise ValueError(f"command_rejected: flag {flag} is not allowed")
        if token.startswith("-n") and token != "-n" and "-n" in allowed_value_flags:
            index += 1
            continue
        if "=" in token:
            if flag not in allowed_value_flags:
                raise ValueError(f"command_rejected: flag {flag} is not allowed")
            if not token.split("=", 1)[1]:
                raise ValueError(f"command_rejected: flag {flag} requires a value")
            index += 1
            continue
        if flag in allowed_value_flags:
            if index + 1 >= len(argv) or argv[index + 1].startswith("-"):
                raise ValueError(f"command_rejected: flag {flag} requires a value")
            index += 2
            continue
        if flag in allowed_boolean_flags:
            index += 1
            continue
        raise ValueError(f"command_rejected: flag {flag} is not allowed")


def _validate_flags(
    trailing: Sequence[str],
    value_flags: set[str],
    boolean_flags: set[str] | None = None,
) -> None:
    boolean_flags = boolean_flags or set()
    index = 0
    while index < len(trailing):
        token = trailing[index]
        if token == "--":
            raise ValueError("command_rejected: -- separator is not allowed")
        if not token.startswith("-"):
            index += 1
            continue

        flag = token.split("=", 1)[0] if "=" in token else token
        if flag in _FORBIDDEN_GLOBAL_FLAGS:
            raise ValueError(f"command_rejected: {flag} changes cluster or identity boundary")
        if token.startswith("-n") and token != "-n" and "-n" in value_flags:
            index += 1
            continue
        if "=" in token:
            if flag not in value_flags:
                raise ValueError(f"command_rejected: flag {flag} is not allowed")
            if not token.split("=", 1)[1]:
                raise ValueError(f"command_rejected: flag {flag} requires a value")
            index += 1
            continue
        if flag in value_flags:
            if index + 1 >= len(trailing) or trailing[index + 1].startswith("-"):
                raise ValueError(f"command_rejected: flag {flag} requires a value")
            index += 2
            continue
        if flag in boolean_flags:
            index += 1
            continue
        raise ValueError(f"command_rejected: flag {flag} is not allowed")


def _parse_resource_and_trailing(argv: Sequence[str], start: int, value_flags: set[str]) -> _ParsedArgv:
    index = start
    while index < len(argv):
        token = argv[index]
        if token.startswith("-"):
            flag = _flag_name(token)
            if token.startswith("-n") and token != "-n" and "-n" in value_flags:
                index += 1
            elif flag in value_flags and "=" not in token and index + 1 < len(argv):
                index += 2
            else:
                index += 1
            continue
        return _ParsedArgv(_resource_token(token), tuple(argv[index + 1 :]))
    return _ParsedArgv(None, ())


def _validate_read_allowlist(argv: Sequence[str]) -> None:
    if len(argv) < 2:
        raise ValueError("command_rejected: missing kubectl subcommand")
    subcommand = argv[1].lower()
    if subcommand in _MUTATING_SUBCOMMANDS:
        raise ValueError(f"command_rejected: kubectl {subcommand} is not allowed on read path")

    if subcommand == "get":
        _validate_complete_argv_flags(argv, _READ_FLAGS_WITH_VALUE, set())
        parsed = _parse_resource_and_trailing(argv, 2, _READ_FLAGS_WITH_VALUE)
        if parsed.resource not in _GET_RESOURCES:
            raise ValueError(f"command_rejected: get {parsed.resource or ''} is not in read allowlist")
        output = _flag_value(argv, {"-o", "--output"})
        if output is not None and output.strip().lower() not in _OUTPUT_FORMATS:
            raise ValueError(f"command_rejected: output format {output} is not allowed")
        _validate_flags(parsed.trailing, _READ_FLAGS_WITH_VALUE)
        return

    if subcommand == "describe":
        _validate_complete_argv_flags(argv, {"-n", "--namespace"}, set())
        parsed = _parse_resource_and_trailing(argv, 2, {"-n", "--namespace"})
        if parsed.resource not in _DESCRIBE_RESOURCES:
            raise ValueError(f"command_rejected: describe {parsed.resource or ''} is not in read allowlist")
        _validate_flags(parsed.trailing, {"-n", "--namespace"})
        return

    if subcommand == "logs":
        _validate_complete_argv_flags(argv, _LOG_FLAGS_WITH_VALUE, _LOG_BOOLEAN_FLAGS)
        parsed = _parse_resource_and_trailing(argv, 2, _LOG_FLAGS_WITH_VALUE)
        if parsed.resource not in {"pod", "pods", "po"}:
            raise ValueError("command_rejected: logs is only allowed for pod resources")
        _validate_flags(parsed.trailing, _LOG_FLAGS_WITH_VALUE, _LOG_BOOLEAN_FLAGS)
        return

    if subcommand == "rollout":
        if len(argv) < 3 or argv[2].lower() not in {"history", "status"}:
            raise ValueError("command_rejected: only rollout history/status is allowed")
        _validate_complete_argv_flags(argv, _ROLLOUT_FLAGS_WITH_VALUE, set())
        parsed = _parse_resource_and_trailing(argv, 3, _ROLLOUT_FLAGS_WITH_VALUE)
        if parsed.resource not in _ROLLOUT_RESOURCES:
            raise ValueError(
                f"command_rejected: rollout {argv[2].lower()} {parsed.resource or ''} is not in read allowlist"
            )
        _validate_flags(parsed.trailing, _ROLLOUT_FLAGS_WITH_VALUE)
        return

    raise ValueError(f"command_rejected: kubectl {subcommand} is not in read allowlist")


def _truncate_text(value: str, limit: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return value, False
    return encoded[:limit].decode("utf-8", errors="replace"), True


def _collect_streaming_output(
    argv: Sequence[str],
    *,
    timeout_seconds: int,
    output_limit_bytes: int,
    popen_factory: Any,
) -> tuple[str, str, int, bool, str | None]:
    process = popen_factory(
        list(argv),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
    )
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    collected_size = 0
    truncated = False
    timed_out = False
    terminated_for_limit = False
    start = time.monotonic()

    selector = DefaultSelector()
    if process.stdout is not None:
        selector.register(process.stdout, EVENT_READ, "stdout")
    if process.stderr is not None:
        selector.register(process.stderr, EVENT_READ, "stderr")

    try:
        while selector.get_map():
            remaining = timeout_seconds - (time.monotonic() - start)
            if remaining <= 0:
                timed_out = True
                process.kill()
                break
            for key, _ in selector.select(timeout=min(0.1, remaining)):
                chunk = key.fileobj.read1(8192) if hasattr(key.fileobj, "read1") else key.fileobj.read(8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target_chunks = stdout_chunks if key.data == "stdout" else stderr_chunks
                remaining_limit = output_limit_bytes - collected_size
                if remaining_limit > 0:
                    target_chunks.append(chunk[:remaining_limit])
                    collected_size += min(len(chunk), remaining_limit)
                if len(chunk) > remaining_limit:
                    truncated = True
                    terminated_for_limit = True
                    process.terminate()
                    break
            if terminated_for_limit:
                break
    finally:
        selector.close()

    if timed_out:
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
        return (
            b"".join(stdout_chunks).decode("utf-8", errors="replace"),
            b"".join(stderr_chunks).decode("utf-8", errors="replace")
            or f"kubectl execution timed out after {timeout_seconds}s",
            -1,
            truncated,
            "timeout",
        )

    if terminated_for_limit:
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
        return (
            b"".join(stdout_chunks).decode("utf-8", errors="replace"),
            b"".join(stderr_chunks).decode("utf-8", errors="replace"),
            process.returncode if isinstance(process.returncode, int) else -1,
            True,
            "output_limit_exceeded",
        )

    exit_code = process.wait(timeout=1)
    return (
        b"".join(stdout_chunks).decode("utf-8", errors="replace"),
        b"".join(stderr_chunks).decode("utf-8", errors="replace"),
        int(exit_code),
        truncated,
        None,
    )


def _validate_argv_namespace(envelope: CommandEnvelope, allowed_namespaces: set[str]) -> None:
    argv = envelope.argv
    index = 1
    while index < len(argv):
        token = argv[index]
        if token == "--all-namespaces" or token.startswith("--all-namespaces="):
            raise ValueError("namespace_out_of_scope: --all-namespaces is not allowed")
        if token == "-A" or token.startswith("-A") or token == "--all-ns" or token.startswith("--all-ns="):
            raise ValueError("namespace_out_of_scope: all namespaces flag is not allowed")

        namespace: str | None = None
        if token in {"-n", "--namespace"}:
            if index + 1 >= len(argv):
                raise ValueError("command_rejected: namespace flag requires a value")
            namespace = argv[index + 1]
            index += 1
        elif token.startswith("-n="):
            namespace = token.split("=", 1)[1]
        elif token.startswith("-n") and token != "-n":
            namespace = token[2:]
        elif token.startswith("--namespace="):
            namespace = token.split("=", 1)[1]

        if namespace is not None:
            if namespace != envelope.namespace:
                raise ValueError("namespace_out_of_scope: argv namespace differs from envelope")
            if namespace not in allowed_namespaces and "*" not in allowed_namespaces:
                raise ValueError("namespace_out_of_scope")
            if not namespace:
                raise ValueError("command_rejected: namespace flag requires a value")

        index += 1


def validate_command_envelope(
    envelope: CommandEnvelope,
    *,
    connector_cluster_id: str,
    allowed_namespaces: set[str],
) -> None:
    """Validate a command envelope without executing kubectl."""
    envelope.validate()
    if envelope.cluster_id != connector_cluster_id:
        raise ValueError("cluster_id does not match connector")
    if envelope.namespace not in allowed_namespaces and "*" not in allowed_namespaces:
        raise ValueError("namespace_out_of_scope")
    if envelope.action_type != "read":
        raise ValueError("command_rejected: action_type must be read")
    if envelope.risk_level not in _LOW_RISK_LEVELS:
        raise ValueError("command_rejected: risk_level must be low or empty")
    if not envelope.grant_id:
        raise ValueError("grant_ref is required")
    if envelope.argv[0] != "kubectl":
        raise ValueError("command_rejected: only kubectl argv is supported")
    if any(token in _SHELL_CONTROL_TOKENS for token in envelope.argv):
        raise ValueError("command_rejected: shell control tokens are not allowed")
    _validate_argv_namespace(envelope, allowed_namespaces)
    _validate_read_allowlist(envelope.argv)
    if envelope.timeout_seconds > MAX_TIMEOUT_SECONDS:
        raise ValueError("command_rejected: timeout exceeds connector limit")
    if envelope.output_limit_bytes > MAX_OUTPUT_LIMIT_BYTES:
        raise ValueError("command_rejected: output limit exceeds connector limit")


def execute_command_envelope(
    envelope: CommandEnvelope,
    *,
    connector_id: str = "connector-local",
    connector_cluster_id: str | None = None,
    allowed_namespaces: set[str] | None = None,
    popen_factory: Any = None,
) -> ResultEnvelope:
    """Execute an approved envelope.

    Calls kubectl with argv only, enforces timeout/output limits, and returns a
    controlled `ResultEnvelope` for both success and rejection paths.
    """
    try:
        validate_command_envelope(
            envelope,
            connector_cluster_id=connector_cluster_id or envelope.cluster_id,
            allowed_namespaces=allowed_namespaces or {envelope.namespace},
        )
    except ValueError as exc:
        message = str(exc)
        code = message.split(":", 1)[0] if ":" in message else "command_rejected"
        return rejected_result(
            envelope,
            connector_id=connector_id,
            error_code=code,
            error_message=message,
        )

    try:
        stdout, stderr, exit_code, truncated, error_code = _collect_streaming_output(
            envelope.argv,
            timeout_seconds=envelope.timeout_seconds,
            output_limit_bytes=envelope.output_limit_bytes,
            popen_factory=popen_factory or subprocess.Popen,
        )
        if error_code is None and exit_code != 0:
            error_code = "execution_failed"
    except FileNotFoundError as exc:
        return _result(
            envelope,
            connector_id=connector_id,
            status="failed",
            stderr=f"kubectl startup failed: {exc}",
            exit_code=-1,
            error_code="backend_unavailable",
            error_message=f"kubectl startup failed: {exc}",
        )

    command = _argv_to_command(envelope.argv)
    redacted_stdout = redact_k8s_output(stdout, command)
    redacted_stderr = redact_k8s_output(stderr, command)
    if hasattr(redacted_stdout, "__await__"):
        import asyncio

        redacted_stdout = asyncio.run(redacted_stdout)
        redacted_stderr = asyncio.run(redacted_stderr)

    limited_stdout, stdout_truncated = _truncate_text(str(redacted_stdout), envelope.output_limit_bytes)
    limited_stderr, stderr_truncated = _truncate_text(str(redacted_stderr), envelope.output_limit_bytes)
    status = "succeeded" if exit_code == 0 and error_code is None else "failed"
    return _result(
        envelope,
        connector_id=connector_id,
        status=status,
        stdout=limited_stdout if status == "succeeded" or error_code == "output_limit_exceeded" else "",
        stderr=limited_stderr if status != "succeeded" else limited_stderr,
        exit_code=exit_code,
        truncated=truncated or stdout_truncated or stderr_truncated,
        error_code=error_code,
        error_message=None if error_code is None else limited_stderr or limited_stdout or error_code,
    )
