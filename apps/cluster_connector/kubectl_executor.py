"""Kubectl executor boundary for approved Connector command envelopes."""

from __future__ import annotations

from aiops.k8s import CommandEnvelope, ResultEnvelope


MAX_TIMEOUT_SECONDS = 600
MAX_OUTPUT_LIMIT_BYTES = 1024 * 1024


def _validate_argv_namespace(envelope: CommandEnvelope, allowed_namespaces: set[str]) -> None:
    argv = envelope.argv
    index = 1
    while index < len(argv):
        token = argv[index]
        if token == "--all-namespaces" or token.startswith("--all-namespaces="):
            raise ValueError("namespace_out_of_scope: --all-namespaces is not allowed")
        if token in {"-A", "--all-ns"}:
            raise ValueError("namespace_out_of_scope: all namespaces flag is not allowed")

        namespace: str | None = None
        if token in {"-n", "--namespace"}:
            if index + 1 >= len(argv):
                raise ValueError("command_rejected: namespace flag requires a value")
            namespace = argv[index + 1]
            index += 1
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
    _validate_argv_namespace(envelope, allowed_namespaces)
    if envelope.timeout_seconds > MAX_TIMEOUT_SECONDS:
        raise ValueError("command_rejected: timeout exceeds connector limit")
    if envelope.output_limit_bytes > MAX_OUTPUT_LIMIT_BYTES:
        raise ValueError("command_rejected: output limit exceeds connector limit")


def execute_command_envelope(_: CommandEnvelope) -> ResultEnvelope:
    """Execute an approved envelope.

    The real implementation will call kubectl without shell expansion, enforce
    timeout and output limits, and return a `ResultEnvelope`.
    """
    raise NotImplementedError("Cluster Connector executor is not implemented yet")
