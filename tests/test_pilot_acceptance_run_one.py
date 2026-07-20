from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiops.acceptance.command import CommandResult
from aiops.acceptance.evidence import GATE_SEQUENCE, AcceptanceEvidence, GateFailed
from tests.pilot_acceptance_support import create_evidence, open_evidence
from aiops.acceptance.http import HttpResponse
from aiops.acceptance.run_one import RunOneGateRunner, V01Inputs
from aiops.acceptance.web_gates import BrowserResult


ADMIN_PASSWORD = "acceptance-admin-password"
SRE_PASSWORD = "acceptance-sre-password"


def _advance_to(evidence: AcceptanceEvidence, gate_id: str) -> None:
    for predecessor in GATE_SEQUENCE[: GATE_SEQUENCE.index(gate_id)]:
        evidence.start_gate(predecessor)
        evidence.record_gate(
            predecessor,
            "not_applicable" if predecessor == "I04" else "passed",
            [],
        )


class FakeCommands:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def run(self, command, **_kwargs) -> CommandResult:
        call = tuple(command)
        self.commands.append(call)
        stdout = ""
        if call[:3] == ("kubectl", "get", "job/verification-trigger"):
            if "--ignore-not-found" not in call:
                stdout = json.dumps({
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "metadata": {
                        "name": "verification-trigger",
                        "namespace": "aiops-verification",
                        "uid": "run-controller-uid-1",
                    },
                    "status": {"succeeded": 1, "startTime": 1000.0},
                })
        elif call[:3] == ("kubectl", "logs", "job/verification-trigger"):
            stdout = '{"event":"verification_trigger_job_succeeded","run_id":"run-controller-uid-1"}\n'
        return CommandResult(call, 0, stdout, "", 0.1)


class FakeConsole:
    def provision_v01(self, **_kwargs) -> BrowserResult:
        return BrowserResult(
            summary={
                "same_origin": True,
                "origins": ["http://pilot.test"],
                "cluster": {
                    "cluster_id": "pilot-cluster",
                    "environment": "test",
                    "mutation_enabled": True,
                },
                "sre": {"id": "user-sre", "username": "pilot-sre"},
                "team": {"id": "team-verification", "name": "Pilot Verification"},
                "service": {"id": "service-verification", "name": "verification-api"},
                "binding": {
                    "id": "binding-verification",
                    "deployment_target_id": "target-verification",
                    "namespace": "aiops-verification",
                    "workload_kind": "Deployment",
                    "workload_name": "verification-api",
                },
                "authority": {
                    "id": "authority-verification",
                    "environment": "test",
                    "scope_type": "namespace",
                    "scope": {
                        "cluster_id": "pilot-cluster",
                        "namespace": "aiops-verification",
                    },
                },
            },
            screenshots={"v01-console.png": b"png"},
        )


class FakeRunSignalProbe:
    def probe_v02(self, run_id: str) -> dict[str, object]:
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


class FakeUserSession:
    def request(self, _method: str, path: str, **_kwargs) -> HttpResponse:
        if path == "/api/v1/incidents":
            return HttpResponse(200, {"incidents": [{
                "id": "incident-run-one",
                "created_at": 1110.0,
                "alertname": "AIOpsVerificationWorkloadUnavailable",
                "cluster_id": "pilot-cluster",
                "namespace": "aiops-verification",
                "workload_kind": "Deployment",
                "workload_name": "verification-api",
            }]}, {})
        if path == "/api/v1/incidents/incident-run-one/workbench":
            return HttpResponse(200, {
                "incident": {"id": "incident-run-one", "created_at": 1110.0},
                "investigation": {
                    "id": "investigation-run-one",
                    "status": "running",
                    "created_at": 1110.0,
                },
                "alert_signals": [{
                    "fingerprint": "run-fingerprint",
                    "alertname": "AIOpsVerificationWorkloadUnavailable",
                    "status": "firing",
                    "workload_kind": "Deployment",
                    "workload_name": "verification-api",
                    "started_at": 1105.0,
                    "created_at": 1105.0,
                    "firing_webhook_request_id": "alertmanager-request-run-one",
                }],
            }, {})
        raise AssertionError(path)


class FakeDiagnosisSession:
    def request(self, _method: str, path: str, **_kwargs) -> HttpResponse:
        if path == "/api/v1/platform/status":
            return HttpResponse(200, {"capabilities": {"model": {
                "readiness": "ready",
                "configuration_revision": "model-provider:revision-1",
                "verification": {
                    "state": "verified",
                    "revision": "model-provider:revision-1",
                },
                "availability": {"state": "available"},
            }}}, {})
        if path == "/api/v1/incidents/incident-run-one/workbench":
            steps = [
                {"id": "step-metrics", "source": "prometheus", "state": "succeeded",
                 "scope": {"cluster_id": "pilot-cluster", "namespace": "aiops-verification",
                           "workload_kind": "Deployment", "workload_name": "verification-api"},
                 "observed_at": 990.0, "expires_at": 1300.0},
                {"id": "step-logs", "source": "loki", "state": "succeeded",
                 "scope": {"cluster_id": "pilot-cluster", "namespace": "aiops-verification",
                           "workload_kind": "Deployment", "workload_name": "verification-api"},
                 "observed_at": 991.0, "expires_at": 1300.0},
                {"id": "step-k8s", "source": "k8s", "state": "succeeded",
                 "scope": {"cluster_id": "pilot-cluster", "namespace": "aiops-verification",
                           "workload_kind": "Deployment", "workload_name": "verification-api"},
                 "observed_at": 992.0, "expires_at": 1300.0},
            ]
            return HttpResponse(200, {
                "incident": {"id": "incident-run-one"},
                "investigation": {
                    "id": "investigation-run-one",
                    "status": "completed",
                    "model_revision": "model-provider:revision-1",
                },
                "alert_signals": [{
                    "fingerprint": "run-fingerprint",
                    "alertname": "AIOpsVerificationWorkloadUnavailable",
                    "status": "firing",
                    "workload_kind": "Deployment",
                    "workload_name": "verification-api",
                }],
                "evidence_steps": steps,
                "judgment": {"evidence_gate_status": "complete", "summary": "latched fault"},
                "recommended_actions": [{
                    "id": "action-run-one",
                    "hash": "c" * 64,
                    "summary": "Restart verification-api through a controlled rollout",
                    "change_intent": "controlled_restart",
                    "target": {"cluster_id": "pilot-cluster", "namespace": "aiops-verification",
                               "workload_kind": "Deployment", "workload_name": "verification-api"},
                    "evidence_step_ids": ["step-metrics", "step-logs", "step-k8s"],
                    "gate": {"status": "complete", "reasons": []},
                    "stale": False,
                }],
            }, {})
        raise AssertionError(path)


def test_v01_uses_fixture_and_console_boundaries_without_persisting_passwords(
    tmp_path: Path,
) -> None:
    evidence = create_evidence(
        tmp_path,
        acceptance_id="v0.1.0-run-one",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        acceptance_tool_sha256="c" * 64,
        gate_contract_revision="pilot-clean-acceptance-v5",
        kube_context="pilot-context",
        cluster_identity_sha256="b" * 64,
        access_profile="http_nodeport",
    )
    _advance_to(evidence, "V01")
    commands = FakeCommands()
    runner = RunOneGateRunner(
        evidence=evidence,
        commands=commands,
        console=FakeConsole(),
        base_url="http://pilot.test",
        now=lambda: 1000.0,
    )

    result = runner.run_v01(
        V01Inputs(
            release_root=tmp_path / "aiops-pilot-v0.1.0",
            admin_username="admin",
            admin_password=ADMIN_PASSWORD,
            sre_username="pilot-sre",
            sre_password=SRE_PASSWORD,
        )
    )

    assert result["run_id"] == "run-controller-uid-1"
    assert result["trigger_started_at"] == 1000.0
    assert [command[:2] for command in commands.commands] == [
        ("kubectl", "apply"),
        ("kubectl", "wait"),
        ("kubectl", "get"),
        ("kubectl", "create"),
        ("kubectl", "wait"),
        ("kubectl", "get"),
        ("kubectl", "logs"),
    ]
    manifest = json.loads(evidence.manifest_path.read_text())
    assert manifest["gates"]["V01"][0]["status"] == "passed"
    persisted = "\n".join(
        path.read_text(errors="ignore")
        for path in evidence.root.rglob("*")
        if path.is_file()
    )
    assert ADMIN_PASSWORD not in persisted
    assert SRE_PASSWORD not in persisted


def test_v01_rejects_a_preexisting_fixed_job_without_dispatching_trigger(
    tmp_path: Path,
) -> None:
    class PreexistingJobCommands(FakeCommands):
        def run(self, command, **kwargs) -> CommandResult:
            result = super().run(command, **kwargs)
            if "--ignore-not-found" in result.command:
                return CommandResult(
                    result.command,
                    0,
                    json.dumps({
                        "metadata": {
                            "name": "verification-trigger",
                            "namespace": "aiops-verification",
                            "uid": "old-controller-uid",
                        },
                        "status": {"succeeded": 1, "startTime": 900.0},
                    }),
                    "",
                    0.1,
                )
            return result

    evidence = create_evidence(
        tmp_path,
        acceptance_id="v0.1.0-v01-existing",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        acceptance_tool_sha256="c" * 64,
        gate_contract_revision="pilot-clean-acceptance-v5",
        kube_context="pilot-context",
        cluster_identity_sha256="b" * 64,
        access_profile="http_nodeport",
    )
    _advance_to(evidence, "V01")
    commands = PreexistingJobCommands()
    runner = RunOneGateRunner(
        evidence=evidence,
        commands=commands,
        console=FakeConsole(),
        base_url="http://pilot.test",
    )

    with pytest.raises(GateFailed, match="V01"):
        runner.run_v01(V01Inputs(
            release_root=tmp_path / "release",
            admin_username="admin",
            admin_password=ADMIN_PASSWORD,
            sre_username="pilot-sre",
            sre_password=SRE_PASSWORD,
        ))

    assert not any(command[:2] == ("kubectl", "create") for command in commands.commands)


def test_v01_interruption_reconciles_the_same_job_without_replaying_create(
    tmp_path: Path,
) -> None:
    class InterruptedCommands(FakeCommands):
        def run(self, command, **kwargs) -> CommandResult:
            if (
                tuple(command)[:2] == ("kubectl", "wait")
                and "job/verification-trigger" in command
            ):
                raise KeyboardInterrupt
            return super().run(command, **kwargs)

    evidence = create_evidence(
        tmp_path,
        acceptance_id="v0.1.0-v01-resume",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        acceptance_tool_sha256="c" * 64,
        gate_contract_revision="pilot-clean-acceptance-v5",
        kube_context="pilot-context",
        cluster_identity_sha256="b" * 64,
        access_profile="http_nodeport",
    )
    _advance_to(evidence, "V01")
    with pytest.raises(KeyboardInterrupt):
        RunOneGateRunner(
            evidence=evidence,
            commands=InterruptedCommands(),
            console=FakeConsole(),
            base_url="http://pilot.test",
        ).run_v01(V01Inputs(
            release_root=tmp_path / "release",
            admin_username="admin",
            admin_password=ADMIN_PASSWORD,
            sre_username="pilot-sre",
            sre_password=SRE_PASSWORD,
        ))

    reopened = open_evidence(evidence.root)
    commands = FakeCommands()
    result = RunOneGateRunner(
        evidence=reopened,
        commands=commands,
        console=FakeConsole(),
        base_url="http://pilot.test",
    ).resume_v01()

    assert result["run_id"] == "run-controller-uid-1"
    assert [command[:3] for command in commands.commands] == [
        ("kubectl", "wait", "-n"),
        ("kubectl", "get", "job/verification-trigger"),
        ("kubectl", "logs", "job/verification-trigger"),
    ]


def test_v02_links_real_signal_paths_to_the_public_incident(tmp_path: Path) -> None:
    evidence = create_evidence(
        tmp_path,
        acceptance_id="v0.1.0-run-one",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        acceptance_tool_sha256="c" * 64,
        gate_contract_revision="pilot-clean-acceptance-v5",
        kube_context="pilot-context",
        cluster_identity_sha256="b" * 64,
        access_profile="http_nodeport",
    )
    _advance_to(evidence, "V02")
    runner = RunOneGateRunner(
        evidence=evidence,
        commands=FakeCommands(),
        console=FakeConsole(),
        base_url="http://pilot.test",
        user=FakeUserSession(),
        telemetry=FakeRunSignalProbe(),
        now=lambda: 1100.0,
        sleep=lambda _seconds: None,
    )

    result = runner.run_v02(
        "run-controller-uid-1", trigger_started_at=1000.0, attempts=1
    )

    assert result == {
        "run_id": "run-controller-uid-1",
        "incident_id": "incident-run-one",
        "investigation_id": "investigation-run-one",
        "alert_fingerprint": "run-fingerprint",
        "webhook_request_id": "alertmanager-request-run-one",
    }
    manifest = json.loads(evidence.manifest_path.read_text())
    assert manifest["gates"]["V02"][0]["status"] == "passed"


def test_v03_requires_fresh_metrics_logs_and_kubernetes_evidence(tmp_path: Path) -> None:
    evidence = create_evidence(
        tmp_path,
        acceptance_id="v0.1.0-run-one",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        acceptance_tool_sha256="c" * 64,
        gate_contract_revision="pilot-clean-acceptance-v5",
        kube_context="pilot-context",
        cluster_identity_sha256="b" * 64,
        access_profile="http_nodeport",
    )
    _advance_to(evidence, "V03")
    runner = RunOneGateRunner(
        evidence=evidence,
        commands=FakeCommands(),
        console=FakeConsole(),
        base_url="http://pilot.test",
        user=FakeDiagnosisSession(),
        now=lambda: 1000.0,
        sleep=lambda _seconds: None,
    )

    result = runner.run_v03(
        run_id="run-controller-uid-1",
        incident_id="incident-run-one",
        investigation_id="investigation-run-one",
        alert_fingerprint="run-fingerprint",
        attempts=1,
    )

    assert result == {
        "run_id": "run-controller-uid-1",
        "incident_id": "incident-run-one",
        "investigation_id": "investigation-run-one",
        "model_revision": "model-provider:revision-1",
        "recommended_action_id": "action-run-one",
        "recommended_action_hash": "c" * 64,
        "recommended_action_summary": "Restart verification-api through a controlled rollout",
    }
    manifest = json.loads(evidence.manifest_path.read_text())
    assert manifest["gates"]["V03"][0]["status"] == "passed"


def test_v03_rejects_current_model_revision_that_differs_from_the_investigation(
    tmp_path: Path,
) -> None:
    evidence = create_evidence(
        tmp_path,
        acceptance_id="v0.1.0-run-one",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        acceptance_tool_sha256="c" * 64,
        gate_contract_revision="pilot-clean-acceptance-v5",
        kube_context="pilot-context",
        cluster_identity_sha256="b" * 64,
        access_profile="http_nodeport",
    )
    _advance_to(evidence, "V03")
    user = FakeDiagnosisSession()
    original = user.request

    def changed_model(method: str, path: str, **kwargs) -> HttpResponse:
        response = original(method, path, **kwargs)
        if path == "/api/v1/platform/status":
            model = response.body["capabilities"]["model"]
            model["configuration_revision"] = "model-provider:revision-2"
            model["verification"]["revision"] = "model-provider:revision-2"
        return response

    user.request = changed_model  # type: ignore[method-assign]
    runner = RunOneGateRunner(
        evidence=evidence,
        commands=FakeCommands(),
        console=FakeConsole(),
        base_url="http://pilot.test",
        user=user,
        now=lambda: 1000.0,
        sleep=lambda _seconds: None,
    )

    with pytest.raises(GateFailed, match="V03"):
        runner.run_v03(
            run_id="run-controller-uid-1",
            incident_id="incident-run-one",
            investigation_id="investigation-run-one",
            alert_fingerprint="run-fingerprint",
            attempts=1,
        )
