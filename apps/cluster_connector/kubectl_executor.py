"""Kubectl executor boundary for approved Connector command envelopes."""

from __future__ import annotations

from aiops.k8s import CommandEnvelope, ResultEnvelope


def execute_command_envelope(_: CommandEnvelope) -> ResultEnvelope:
    """Execute an approved envelope.

    The real implementation will call kubectl without shell expansion, enforce
    timeout and output limits, and return a `ResultEnvelope`.
    """
    raise NotImplementedError("Cluster Connector executor is not implemented yet")
