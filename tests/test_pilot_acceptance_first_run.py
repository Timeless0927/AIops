from __future__ import annotations

from pathlib import Path

import pytest

from aiops.acceptance.evidence import GATE_SEQUENCE, AcceptanceEvidence, GateFailed
from tests.pilot_acceptance_support import create_evidence, open_evidence
from aiops.acceptance.http import HttpResponse
from aiops.acceptance.run_one import RunOneGateRunner


def _ledger(tmp_path: Path, name: str) -> AcceptanceEvidence:
    evidence = create_evidence(
        tmp_path,
        acceptance_id=name,
        release_version="v0.1.0",
        release_sha256="a" * 64,
        acceptance_tool_sha256="c" * 64,
        gate_contract_revision="pilot-clean-acceptance-v3",
        kube_context="pilot-context",
        cluster_identity_sha256="b" * 64,
        access_profile="http_nodeport",
    )
    for gate_id in GATE_SEQUENCE[: GATE_SEQUENCE.index("V02")]:
        evidence.start_gate(gate_id)
        evidence.record_gate(
            gate_id,
            "not_applicable" if gate_id == "I04" else "passed",
            [],
        )
    return evidence


class _Unused:
    def run(self, *_args, **_kwargs):
        raise AssertionError("command Adapter must not be used by V02")

    def provision_v01(self, **_kwargs):
        raise AssertionError("Console Adapter must not be used by V02")


class _Telemetry:
    def __init__(self, *, clock: list[float] | None = None, finish_at: float = 0) -> None:
        self.calls = 0
        self.clock = clock
        self.finish_at = finish_at

    def probe_v02(self, run_id: str) -> dict[str, object]:
        self.calls += 1
        if self.clock is not None:
            self.clock[0] = self.finish_at
        return {
            "run_id": run_id,
            "observed_at": 1100.0,
            "fault_metric_series": 1,
            "deployment_unavailable_series": 1,
            "activation_log_lines": 1,
            "prometheus_alerts": [{"labels_sha256": "1" * 64, "state": "firing"}],
            "alertmanager_alerts": [{
                "fingerprint": "run-fingerprint",
                "labels_sha256": "1" * 64,
                "status": "active",
            }],
        }


class _PublicSession:
    def __init__(
        self,
        *,
        created_at: float = 1110.0,
        empty_reads: int = 0,
        interrupt: bool = False,
        clock: list[float] | None = None,
        query_at: float = 0,
    ) -> None:
        self.created_at = created_at
        self.empty_reads = empty_reads
        self.interrupt = interrupt
        self.clock = clock
        self.query_at = query_at
        self.incident_reads = 0

    def request(self, _method: str, path: str, **_kwargs) -> HttpResponse:
        if path == "/api/v1/incidents":
            self.incident_reads += 1
            if self.interrupt:
                raise KeyboardInterrupt
            if self.clock is not None:
                self.clock[0] = self.query_at
            if self.incident_reads <= self.empty_reads:
                return HttpResponse(200, {"incidents": []}, {})
            return HttpResponse(200, {"incidents": [{
                "id": "incident-1",
                "created_at": self.created_at,
                "alertname": "AIOpsVerificationWorkloadUnavailable",
                "cluster_id": "pilot-cluster",
                "namespace": "aiops-verification",
                "workload_kind": "Deployment",
                "workload_name": "verification-api",
            }]}, {})
        if path == "/api/v1/incidents/incident-1/workbench":
            return HttpResponse(200, {
                "incident": {"id": "incident-1", "created_at": self.created_at},
                "investigation": {
                    "id": "investigation-1",
                    "status": "running",
                    "created_at": self.created_at,
                },
                "alert_signals": [{
                    "fingerprint": "run-fingerprint",
                    "alertname": "AIOpsVerificationWorkloadUnavailable",
                    "status": "firing",
                    "workload_kind": "Deployment",
                    "workload_name": "verification-api",
                    "started_at": self.created_at,
                    "created_at": self.created_at,
                    "firing_webhook_request_id": "webhook-request-1",
                }],
            }, {})
        raise AssertionError(path)


def _runner(
    evidence: AcceptanceEvidence,
    *,
    user: object,
    telemetry: object | None,
    now,
    monotonic,
) -> RunOneGateRunner:
    unused = _Unused()
    return RunOneGateRunner(
        evidence=evidence,
        commands=unused,
        console=unused,
        base_url="http://pilot.test",
        user=user,
        telemetry=telemetry,
        now=now,
        monotonic=monotonic,
        sleep=lambda _seconds: None,
    )


def test_v02_caches_telemetry_success_while_public_facts_converge(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path, "v02-cache")
    telemetry = _Telemetry()
    result = _runner(
        evidence,
        user=_PublicSession(empty_reads=1),
        telemetry=telemetry,
        now=lambda: 1100.0,
        monotonic=lambda: 0.0,
    ).run_v02("run-1", trigger_started_at=1000.0, attempts=2)

    assert result["webhook_request_id"] == "webhook-request-1"
    assert telemetry.calls == 1


@pytest.mark.parametrize(
    ("telemetry_finish", "public_query"),
    [(121.0, 0.0), (100.0, 181.0)],
    ids=["telemetry-120s", "public-180s"],
)
def test_v02_enforces_separate_monotonic_deadlines(
    tmp_path: Path, telemetry_finish: float, public_query: float
) -> None:
    clock = [0.0]
    evidence = _ledger(tmp_path, f"v02-deadline-{int(telemetry_finish)}")
    runner = _runner(
        evidence,
        user=_PublicSession(clock=clock, query_at=public_query),
        telemetry=_Telemetry(clock=clock, finish_at=telemetry_finish),
        now=lambda: 1000.0,
        monotonic=lambda: clock[0],
    )

    with pytest.raises(GateFailed, match="V02"):
        runner.run_v02("run-1", trigger_started_at=1000.0, attempts=1)


@pytest.mark.parametrize(
    ("created_at", "passes"),
    [(1110.0, True), (1181.0, False)],
    ids=["terminal-before-original-deadline", "fact-after-original-deadline"],
)
def test_v02_resume_uses_persisted_telemetry_and_original_deadline(
    tmp_path: Path, created_at: float, passes: bool
) -> None:
    evidence = _ledger(tmp_path, f"v02-resume-{int(created_at)}")
    telemetry = _Telemetry()
    with pytest.raises(KeyboardInterrupt):
        _runner(
            evidence,
            user=_PublicSession(interrupt=True),
            telemetry=telemetry,
            now=lambda: 1100.0,
            monotonic=lambda: 0.0,
        ).run_v02("run-1", trigger_started_at=1000.0, attempts=1)
    assert telemetry.calls == 1

    reopened = open_evidence(evidence.root)
    runner = _runner(
        reopened,
        user=_PublicSession(created_at=created_at),
        telemetry=None,
        now=lambda: 1300.0,
        monotonic=lambda: 0.0,
    )
    if passes:
        assert runner.resume_v02(attempts=1)["incident_id"] == "incident-1"
        assert reopened.status()["frontier"] == "V03"
    else:
        with pytest.raises(GateFailed, match="V02"):
            runner.resume_v02(attempts=1)
        assert reopened.status()["status"] == "ineligible"
