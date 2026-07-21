from __future__ import annotations

import json

import pytest

from aiops.acceptance.command import CommandResult
from aiops.acceptance.recovery_telemetry import KubectlRetentionTelemetry
from aiops.acceptance.recovery import recovery_metric_ref_sha256


class Commands:
    def __init__(self, *, retained: bool = True, valid_log: bool = True) -> None:
        metric = [{
            "metric": {
                "namespace": "aiops-verification",
                "deployment": "verification-api",
                "service": "verification-api",
                "run_id": "run-1",
            },
            "values": [[1310.0, "0"]],
        }] if retained else []
        logs = [{
            "stream": {
                "namespace": "aiops-verification",
                "container": "verification-api",
            },
            "values": [["1305000000000", (
                '{"event":"verification_fault_recovered","run_id":"run-1"}'
                if valid_log else '{"event":"other","run_id":"run-1"}'
            )]],
        }] if retained else []
        self.responses = [
            {"data": {"result": metric}},
            {"data": {"result": logs}},
        ]

    def run(self, command, **_kwargs) -> CommandResult:
        return CommandResult(
            tuple(command), 0, json.dumps(self.responses.pop(0)), "", 0.1,
        )


def test_retention_probe_requeries_fixed_v06_metric_and_log_windows() -> None:
    value = KubectlRetentionTelemetry(Commands(), kube_context="pilot-context").probe_retention(
        "run-1", metric_at=1310.0, log_at=1305.0,
    )

    assert value["recovery_metric_observed_at"] == 1310.0
    assert value["recovery_log_observed_at"] == 1305.0
    assert len(value["recovery_metric_ref_hashes"]) == 1
    assert value["recovery_metric_ref_hashes"] == [
        recovery_metric_ref_sha256("run-1", 1310.0),
    ]
    assert len(value["recovery_log_ref_hashes"]) == 1
    assert "verification_fault_recovered" not in json.dumps(value)
    assert len(value["commands"]) == 2


def test_retention_probe_fails_when_fixed_samples_disappeared() -> None:
    with pytest.raises(RuntimeError, match="not retained"):
        KubectlRetentionTelemetry(
            Commands(retained=False), kube_context="pilot-context",
        ).probe_retention(
            "run-1", metric_at=1310.0, log_at=1305.0,
        )


def test_retention_probe_rejects_a_loki_line_that_does_not_match_the_run_event() -> None:
    with pytest.raises(RuntimeError, match="not retained"):
        KubectlRetentionTelemetry(
            Commands(valid_log=False), kube_context="pilot-context",
        ).probe_retention("run-1", metric_at=1310.0, log_at=1305.0)
