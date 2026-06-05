"""Kubectl executor boundary for approved Connector command envelopes."""

from __future__ import annotations

from aiops.k8s import CommandEnvelope, ResultEnvelope


MAX_TIMEOUT_SECONDS = 600
MAX_OUTPUT_LIMIT_BYTES = 1024 * 1024


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
