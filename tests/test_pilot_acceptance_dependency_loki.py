from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiops.acceptance.command import CommandResult
from aiops.acceptance.dependency_loki import KubectlLokiDependencyProbe
from aiops.acceptance.http import HttpResponse
from aiops.acceptance.recovery import load_recovery_scope
from tests.pilot_acceptance_recovery_support import recovery_ledger


def _envelope(*, status: str, matched: int = 0) -> dict[str, object]:
    if status == "failed":
        return {
            "status": "failed",
            "errors": [{"code": "backend_unavailable", "message": "down"}],
            "evidence_refs": [],
        }
    return {
        "status": "succeeded",
        "data": {"total_matched": matched},
        "evidence_refs": ([{"source": "loki", "ref_id": f"ref-{matched}"}] if matched else []),
    }


class Commands:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = responses
        self.commands: list[tuple[str, ...]] = []

    def run(self, command, **_kwargs) -> CommandResult:
        value = tuple(command)
        self.commands.append(value)
        assert "sh" not in value and value[-2].startswith("import json,sys,urllib.request")
        assert "Bearer" not in value[-1] and "token" not in value[-1].lower()
        return CommandResult(value, 0, json.dumps(self.responses.pop(0)), "", 0.1)


class Gateway:
    def __init__(self, *, status: int = 200) -> None:
        self.requests: list[tuple[str, str, str]] = []
        self.status = status

    def request(self, method: str, path: str, *, request_id: str) -> HttpResponse:
        self.requests.append((method, path, request_id))
        return HttpResponse(
            self.status,
            {"service": "aiops-k8s-gateway", "status": "ok" if self.status == 200 else "down"},
            {},
        )


def test_loki_dependency_probe_returns_bounded_unavailable_without_evidence(
    tmp_path: Path,
) -> None:
    scope = load_recovery_scope(recovery_ledger(tmp_path, "R03"))
    probe = KubectlLokiDependencyProbe(
        Commands([_envelope(status="failed")]), Gateway(), kube_context="pilot-context",
    )

    result = probe.unavailable(scope, request_id="r04/execution/unavailable")

    assert result == {
        "status": "failed", "error_code": "backend_unavailable", "evidence_refs": [],
    }


def test_loki_dependency_probe_queries_retained_and_fresh_real_logs(tmp_path: Path) -> None:
    scope = load_recovery_scope(recovery_ledger(tmp_path, "R03"))
    commands = Commands([
        _envelope(status="succeeded", matched=1),
        _envelope(status="succeeded", matched=0),
        _envelope(status="succeeded", matched=1),
    ])
    gateway = Gateway()
    now = [0.0]
    probe = KubectlLokiDependencyProbe(
        commands, gateway, kube_context="pilot-context",
        monotonic=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )

    result = probe.recovered(scope, request_id="r04/execution/verify-logs")

    assert result["retained_log_refs_sha256"] == scope.recovery_log_refs_sha256
    assert result["retained_query"]["evidence_ref"]["source"] == "loki"  # type: ignore[index]
    assert result["fresh_query"]["matched"] == 1  # type: ignore[index]
    assert gateway.requests == [("GET", "/healthz", result["fresh_probe_request_id"])]
    bodies = [json.loads(item[-1]) for item in commands.commands]
    assert "verification_fault_recovered" in bodies[0]["query"]
    assert result["fresh_probe_request_id"] in bodies[-1]["query"]


def test_loki_dependency_probe_rejects_failed_envelope_with_evidence(tmp_path: Path) -> None:
    scope = load_recovery_scope(recovery_ledger(tmp_path, "R03"))
    invalid = _envelope(status="failed")
    invalid["evidence_refs"] = [{"source": "loki", "ref_id": "fabricated"}]
    probe = KubectlLokiDependencyProbe(
        Commands([invalid]), Gateway(), kube_context="pilot-context",
    )

    with pytest.raises(ValueError, match="bounded"):
        probe.unavailable(scope, request_id="r04/execution/unavailable")


@pytest.mark.parametrize(
    "envelope",
    [
        {"status": "succeeded", "data": {"total_matched": 1}, "evidence_refs": []},
        {
            "status": "succeeded", "data": {"total_matched": 1},
            "evidence_refs": [
                {"source": "loki", "ref_id": "one"},
                {"source": "loki", "ref_id": "two"},
            ],
        },
        {
            "status": "succeeded", "data": {"total_matched": 1},
            "evidence_refs": [{"source": "prometheus", "ref_id": "wrong"}],
        },
        {
            "status": "succeeded", "data": {"total_matched": 1},
            "evidence_refs": [{"source": "loki", "ref_id": ""}],
        },
        _envelope(status="succeeded", matched=0),
    ],
)
def test_loki_dependency_probe_rejects_unproved_success_envelope(
    tmp_path: Path, envelope: dict[str, object],
) -> None:
    scope = load_recovery_scope(recovery_ledger(tmp_path, "R03"))
    ticks = iter([0.0, 121.0])
    probe = KubectlLokiDependencyProbe(
        Commands([envelope]), Gateway(),
        kube_context="pilot-context", monotonic=lambda: next(ticks), sleep=lambda _seconds: None,
    )

    with pytest.raises(TimeoutError, match="did not become queryable"):
        probe.recovered(scope, request_id="r04/execution/verify-logs")


def test_loki_dependency_probe_requires_real_gateway_fresh_log_trigger(
    tmp_path: Path,
) -> None:
    scope = load_recovery_scope(recovery_ledger(tmp_path, "R03"))
    commands = Commands([])
    probe = KubectlLokiDependencyProbe(
        commands, Gateway(status=503), kube_context="pilot-context",
    )

    with pytest.raises(RuntimeError, match="fresh Gateway log probe failed"):
        probe.recovered(scope, request_id="r04/execution/verify-logs")
    assert commands.commands == []
