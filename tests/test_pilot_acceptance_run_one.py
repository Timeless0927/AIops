from __future__ import annotations

import json
from pathlib import Path

from aiops.acceptance.command import CommandResult
from aiops.acceptance.evidence import AcceptanceEvidence
from aiops.acceptance.http import HttpResponse
from aiops.acceptance.run_one import RunOneGateRunner, V01Inputs
from aiops.acceptance.web_gates import BrowserResult


ADMIN_PASSWORD = "acceptance-admin-password"
SRE_PASSWORD = "acceptance-sre-password"


class FakeCommands:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def run(self, command, **_kwargs) -> CommandResult:
        call = tuple(command)
        self.commands.append(call)
        stdout = ""
        if call[:3] == ("kubectl", "get", "job/verification-trigger"):
            stdout = json.dumps({
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {
                    "name": "verification-trigger",
                    "namespace": "aiops-verification",
                    "uid": "run-controller-uid-1",
                },
                "status": {"succeeded": 1},
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
            "fault_metric_series": 1,
            "deployment_unavailable_series": 1,
            "activation_log_lines": 1,
            "prometheus_alerts": [{"fingerprint": "run-fingerprint", "state": "firing"}],
            "alertmanager_alerts": [{"fingerprint": "run-fingerprint", "status": "active"}],
        }


class FakeUserSession:
    def request(self, _method: str, path: str, **_kwargs) -> HttpResponse:
        if path == "/api/v1/incidents":
            return HttpResponse(200, {"incidents": [{
                "id": "incident-run-one",
                "alertname": "AIOpsVerificationWorkloadUnavailable",
                "cluster_id": "pilot-cluster",
                "namespace": "aiops-verification",
                "workload_name": "verification-api",
            }]}, {})
        if path == "/api/v1/incidents/incident-run-one/workbench":
            return HttpResponse(200, {
                "incident": {"id": "incident-run-one"},
                "investigation": {"id": "investigation-run-one", "status": "running"},
                "alert_signals": [{
                    "fingerprint": "run-fingerprint",
                    "alertname": "AIOpsVerificationWorkloadUnavailable",
                    "status": "firing",
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
                "investigation": {"id": "investigation-run-one", "status": "completed"},
                "alert_signals": [{"fingerprint": "run-fingerprint", "status": "firing"}],
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


class FakeChangeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object] | None]] = []

    def request(self, method: str, path: str, **kwargs) -> HttpResponse:
        body = kwargs.get("body")
        self.calls.append((method, path, body))
        annotation = "/spec/template/metadata/annotations/aiops.dev~1verification-run-id"
        draft = {
            "target": {
                "api_version": "apps/v1", "kind": "Deployment",
                "namespace": "aiops-verification", "name": "verification-api",
            },
            "operation": "patch",
            "payload": [{"op": "add", "path": annotation, "value": "run-controller-uid-1"}],
            "post_checks": [
                {"type": "json_pointer", "path": annotation, "operator": "eq", "value": "run-controller-uid-1"},
                {"type": "workload_rollout"},
            ],
            "rollback": {
                "status": "unavailable",
                "concrete_loss": "A rollout cannot restore the previous Pod identities.",
            },
        }
        if method == "POST" and path == "/api/v1/incidents/incident-run-one/change-requests":
            return HttpResponse(201, {"change_request": {"id": "change-run-one"}}, {})
        if method == "GET" and path == "/api/v1/change-requests/change-run-one":
            return HttpResponse(200, {"change_request": {
                "id": "change-run-one",
                "status": "awaiting_approval",
                "active_phase": {"id": "phase-run-one", "status": "awaiting_approval"},
                "active_revision": {
                    "id": "revision-run-one", "revision_number": 1,
                    "plan": {"summary": "controlled rollout", "changes": [draft]},
                    "validation": {"status": "succeeded"},
                },
            }}, {})
        if method == "GET" and path == "/api/v1/change-requests/change-run-one/phase-approval":
            canonical = {
                "target": {
                    **draft["target"], "uid": "deployment-uid", "resource_version": "42",
                },
                "operation": "patch",
                "payload": [
                    {"op": "test", "path": "/metadata/uid", "value": "deployment-uid"},
                    {"op": "test", "path": "/metadata/resourceVersion", "value": "42"},
                    {"op": "add", "path": "/spec/template/metadata/annotations", "value": {}},
                    *draft["payload"],
                ],
                "post_checks": draft["post_checks"],
            }
            return HttpResponse(200, {"phase_review": {
                "change_request_id": "change-run-one",
                "phase_id": "phase-run-one",
                "revision_id": "revision-run-one",
                "status": "awaiting_approval",
                "changes": [{
                    "ordinal": 1,
                    "target": canonical["target"],
                    "target_confirmation": "apps/v1:Deployment:aiops-verification/verification-api",
                    "operation": "patch",
                    "canonical_change": canonical,
                    "inverse_change": None,
                    "rollback": draft["rollback"],
                    "diff": [{"op": "add", "path": annotation, "before": None, "after": "run-controller-uid-1"}],
                    "dry_run_hash": "d" * 64,
                    "post_checks": draft["post_checks"],
                    "authority_id": "authority-run-one",
                }],
            }}, {})
        if method == "POST" and path == "/auth/reauth":
            return HttpResponse(200, {"status": "fresh"}, {})
        if method == "POST" and path == "/api/v1/change-requests/change-run-one/phase-approval/approve":
            return HttpResponse(201, {"phase_review": {
                "change_request_id": "change-run-one",
                "phase_id": "phase-run-one",
                "revision_id": "revision-run-one",
                "status": "approved",
                "changes": [],
                "approval": {
                    "id": "approval-run-one",
                    "authority_ids": ["authority-run-one"],
                    "rollback_policy": "stop_only",
                    "frozen_changes": [{
                        "dry_run_hash": "d" * 64,
                        "target_confirmation": "apps/v1:Deployment:aiops-verification/verification-api",
                    }],
                },
            }}, {})
        if method == "POST" and path == "/api/v1/change-requests/change-run-one/phase-execution/start":
            return HttpResponse(201, {"phase_execution": {
                "id": "execution-run-one",
                "change_request_id": "change-run-one",
                "phase_id": "phase-run-one",
                "approval_id": "approval-run-one",
                "command_id": "command-run-one",
                "status": "queued",
                "grant": {
                    "id": "grant-run-one", "issued_at": 1000.0, "expires_at": 1060.0,
                    "consumed_at": None, "revoked_at": None,
                },
                "steps": [],
            }}, {})
        if method == "GET" and path == "/api/v1/change-requests/change-run-one/phase-execution":
            return HttpResponse(200, {"phase_execution": {
                "id": "execution-run-one",
                "change_request_id": "change-run-one",
                "phase_id": "phase-run-one",
                "approval_id": "approval-run-one",
                "command_id": "command-run-one",
                "status": "succeeded",
                "grant": {
                    "id": "grant-run-one", "issued_at": 1000.0, "expires_at": 1060.0,
                    "consumed_at": 1001.0, "revoked_at": None,
                },
                "steps": [{
                    "id": "execution-step-run-one", "ordinal": 1, "direction": "forward",
                    "command_id": "command-run-one", "status": "succeeded",
                    "result": {"post_checks": [{"status": "succeeded"}, {"status": "succeeded"}]},
                    "grant": {
                        "id": "grant-run-one", "issued_at": 1000.0, "expires_at": 1060.0,
                        "consumed_at": 1001.0, "revoked_at": None,
                    },
                }],
            }}, {})
        raise AssertionError((method, path))


def test_v01_uses_fixture_and_console_boundaries_without_persisting_passwords(
    tmp_path: Path,
) -> None:
    evidence = AcceptanceEvidence.create(
        tmp_path,
        acceptance_id="v0.1.0-run-one",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        kube_context="pilot-context",
        cluster_identity_sha256="b" * 64,
        access_profile="http_nodeport",
    )
    commands = FakeCommands()
    runner = RunOneGateRunner(
        evidence=evidence,
        commands=commands,
        console=FakeConsole(),
        base_url="http://pilot.test",
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
    assert [command[:2] for command in commands.commands] == [
        ("kubectl", "apply"),
        ("kubectl", "wait"),
        ("kubectl", "apply"),
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


def test_v02_links_real_signal_paths_to_the_public_incident(tmp_path: Path) -> None:
    evidence = AcceptanceEvidence.create(
        tmp_path,
        acceptance_id="v0.1.0-run-one",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        kube_context="pilot-context",
        cluster_identity_sha256="b" * 64,
        access_profile="http_nodeport",
    )
    evidence.record_gate("V01", "passed", [])
    runner = RunOneGateRunner(
        evidence=evidence,
        commands=FakeCommands(),
        console=FakeConsole(),
        base_url="http://pilot.test",
        user=FakeUserSession(),
        telemetry=FakeRunSignalProbe(),
        sleep=lambda _seconds: None,
    )

    result = runner.run_v02("run-controller-uid-1", attempts=1)

    assert result == {
        "run_id": "run-controller-uid-1",
        "incident_id": "incident-run-one",
        "investigation_id": "investigation-run-one",
        "alert_fingerprint": "run-fingerprint",
    }
    manifest = json.loads(evidence.manifest_path.read_text())
    assert manifest["gates"]["V02"][0]["status"] == "passed"


def test_v03_requires_fresh_metrics_logs_and_kubernetes_evidence(tmp_path: Path) -> None:
    evidence = AcceptanceEvidence.create(
        tmp_path,
        acceptance_id="v0.1.0-run-one",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        kube_context="pilot-context",
        cluster_identity_sha256="b" * 64,
        access_profile="http_nodeport",
    )
    evidence.record_gate("V01", "passed", [])
    evidence.record_gate("V02", "passed", [])
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


def test_v04_creates_exact_controlled_verification_patch_from_recommendation(tmp_path: Path) -> None:
    evidence = AcceptanceEvidence.create(
        tmp_path,
        acceptance_id="v0.1.0-run-one",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        kube_context="pilot-context",
        cluster_identity_sha256="b" * 64,
        access_profile="http_nodeport",
    )
    for gate in ("V01", "V02", "V03"):
        evidence.record_gate(gate, "passed", [])
    user = FakeChangeSession()
    runner = RunOneGateRunner(
        evidence=evidence,
        commands=FakeCommands(),
        console=FakeConsole(),
        base_url="http://pilot.test",
        user=user,
        sleep=lambda _seconds: None,
    )

    result = runner.run_v04(
        run_id="run-controller-uid-1",
        incident_id="incident-run-one",
        recommended_action_id="action-run-one",
        recommended_action_hash="c" * 64,
        recommended_action_summary="Restart verification-api through a controlled rollout",
        attempts=1,
    )

    assert result == {
        "run_id": "run-controller-uid-1",
        "incident_id": "incident-run-one",
        "change_request_id": "change-run-one",
        "phase_id": "phase-run-one",
        "revision_id": "revision-run-one",
        "dry_run_hash": "d" * 64,
        "target_confirmation": "apps/v1:Deployment:aiops-verification/verification-api",
    }
    create = user.calls[0]
    assert create[2]["desired_outcome"] == "Restart verification-api through a controlled rollout"
    assert "run_id=run-controller-uid-1" in str(create[2]["context"])
    assert "action-run-one" in str(create[2]["context"])
    manifest = json.loads(evidence.manifest_path.read_text())
    assert manifest["gates"]["V04"][0]["status"] == "passed"


def test_v05_requires_attested_exact_approval_and_one_successful_execution(tmp_path: Path) -> None:
    evidence = AcceptanceEvidence.create(
        tmp_path,
        acceptance_id="v0.1.0-run-one",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        kube_context="pilot-context",
        cluster_identity_sha256="b" * 64,
        access_profile="http_nodeport",
        attestation_verifier=lambda _item: None,
    )
    for gate in ("V01", "V02", "V03", "V04"):
        evidence.record_gate(gate, "passed", [])
    statement = evidence.attestation_statement(
        actor="A02 Verification SRE",
        role="sre",
        gate_ids=["V05"],
        conclusion="passed",
        note="exact dry-run diff and unavailable rollback confirmed",
    )
    evidence.append_attestation(
        statement,
        signature="signature",
        public_key="ssh-ed25519 test",
        fingerprint="SHA256:test",
    )
    user = FakeChangeSession()
    runner = RunOneGateRunner(
        evidence=evidence,
        commands=FakeCommands(),
        console=FakeConsole(),
        base_url="http://pilot.test",
        user=user,
        sleep=lambda _seconds: None,
    )

    result = runner.run_v05(
        run_id="run-controller-uid-1",
        incident_id="incident-run-one",
        change_request_id="change-run-one",
        phase_id="phase-run-one",
        revision_id="revision-run-one",
        dry_run_hash="d" * 64,
        target_confirmation="apps/v1:Deployment:aiops-verification/verification-api",
        sre_password="acceptance-sre-password",
        attempts=1,
    )

    assert result == {
        "run_id": "run-controller-uid-1",
        "incident_id": "incident-run-one",
        "change_request_id": "change-run-one",
        "phase_id": "phase-run-one",
        "revision_id": "revision-run-one",
        "approval_id": "approval-run-one",
        "execution_id": "execution-run-one",
        "grant_id": "grant-run-one",
        "command_id": "command-run-one",
    }
    approve = next(call for call in user.calls if call[1].endswith("/phase-approval/approve"))
    assert approve[2]["dry_run_hashes"] == ["d" * 64]
    assert approve[2]["target_confirmations"] == [
        "apps/v1:Deployment:aiops-verification/verification-api"
    ]
    assert approve[2]["rollback_policy"] == "stop_only"
    assert not any(
        "acceptance-sre-password" in json.dumps(call, sort_keys=True)
        for call in user.calls
        if call[1] != "/auth/reauth"
    )
