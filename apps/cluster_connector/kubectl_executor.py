"""Kubectl executor boundary for approved Connector command envelopes."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
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


def _resource_after(argv: Sequence[str], start: int, value_flags: set[str]) -> tuple[str | None, int]:
    index = start
    while index < len(argv):
        token = argv[index]
        if token.startswith("-"):
            flag = token.split("=", 1)[0] if "=" in token else token
            if flag in value_flags and "=" not in token and index + 1 < len(argv):
                index += 2
            else:
                index += 1
            continue
        return _resource_token(token), index + 1
    return None, index


def _validate_read_allowlist(argv: Sequence[str]) -> None:
    if len(argv) < 2:
        raise ValueError("command_rejected: missing kubectl subcommand")
    subcommand = argv[1].lower()
    if subcommand in _MUTATING_SUBCOMMANDS:
        raise ValueError(f"command_rejected: kubectl {subcommand} is not allowed on read path")

    if subcommand == "get":
        resource, trailing_index = _resource_after(argv, 2, _READ_FLAGS_WITH_VALUE)
        if resource not in _GET_RESOURCES:
            raise ValueError(f"command_rejected: get {resource or ''} is not in read allowlist")
        output = _flag_value(argv, {"-o", "--output"})
        if output is not None and output.strip().lower() not in _OUTPUT_FORMATS:
            raise ValueError(f"command_rejected: output format {output} is not allowed")
        _validate_flags(argv[trailing_index:], _READ_FLAGS_WITH_VALUE)
        return

    if subcommand == "describe":
        resource, trailing_index = _resource_after(argv, 2, {"-n", "--namespace"})
        if resource not in _DESCRIBE_RESOURCES:
            raise ValueError(f"command_rejected: describe {resource or ''} is not in read allowlist")
        _validate_flags(argv[trailing_index:], {"-n", "--namespace"})
        return

    if subcommand == "logs":
        resource, trailing_index = _resource_after(argv, 2, _LOG_FLAGS_WITH_VALUE)
        if resource not in {"pod", "pods", "po"}:
            raise ValueError("command_rejected: logs is only allowed for pod resources")
        _validate_flags(argv[trailing_index:], _LOG_FLAGS_WITH_VALUE, _LOG_BOOLEAN_FLAGS)
        return

    if subcommand == "rollout":
        if len(argv) < 3 or argv[2].lower() not in {"history", "status"}:
            raise ValueError("command_rejected: only rollout history/status is allowed")
        resource, trailing_index = _resource_after(argv, 3, _ROLLOUT_FLAGS_WITH_VALUE)
        if resource not in _ROLLOUT_RESOURCES:
            raise ValueError(f"command_rejected: rollout {argv[2].lower()} {resource or ''} is not in read allowlist")
        _validate_flags(argv[trailing_index:], _ROLLOUT_FLAGS_WITH_VALUE)
        return

    raise ValueError(f"command_rejected: kubectl {subcommand} is not in read allowlist")


def _truncate_text(value: str, limit: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return value, False
    return encoded[:limit].decode("utf-8", errors="replace"), True


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
    if not envelope.grant_id:
        raise ValueError("grant_ref is required")
    if envelope.argv[0] != "kubectl":
        raise ValueError("command_rejected: only kubectl argv is supported")
    if any(token in {";", "&&", "||", "|", "$(", "`"} for token in envelope.argv):
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
    runner: Any = None,
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
        run = runner or subprocess.run
        completed = run(
            list(envelope.argv),
            capture_output=True,
            text=True,
            timeout=envelope.timeout_seconds,
            check=False,
        )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        exit_code = int(completed.returncode)
        error_code = None if exit_code == 0 else "execution_failed"
    except subprocess.TimeoutExpired as exc:
        stdout = (
            exc.stdout.decode("utf-8", errors="replace")
            if isinstance(exc.stdout, bytes)
            else str(exc.stdout or "")
        )
        stderr = (
            exc.stderr.decode("utf-8", errors="replace")
            if isinstance(exc.stderr, bytes)
            else str(exc.stderr or "")
        )
        stderr = stderr or f"kubectl execution timed out after {envelope.timeout_seconds}s"
        exit_code = -1
        error_code = "timeout"
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
        stdout=limited_stdout if status == "succeeded" else "",
        stderr=limited_stderr if status != "succeeded" else limited_stderr,
        exit_code=exit_code,
        truncated=stdout_truncated or stderr_truncated,
        error_code=error_code,
        error_message=None if error_code is None else limited_stderr or limited_stdout or error_code,
    )
