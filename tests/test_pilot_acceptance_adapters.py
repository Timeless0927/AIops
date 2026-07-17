from __future__ import annotations

import json
import shutil
import subprocess
import urllib.request
from itertools import count
from pathlib import Path

import pytest

from aiops.acceptance import adapters
from aiops.acceptance.adapters import (
    HttpsProfileProbe,
    OpenSshSigner,
    PlaywrightBrowser,
    PlaywrightV01Console,
)
from aiops.acceptance.command import CommandResult, SubprocessCommands
from aiops.acceptance.evidence import GATE_CONTRACT_REVISION, GATE_SEQUENCE, AcceptanceEvidence
from aiops.acceptance.telemetry import KubernetesTelemetryProbe


class BrowserCommands:
    command = None
    stdin = None

    def run(self, command, *, stdin=None, **_kwargs):
        self.command = tuple(command)
        self.stdin = stdin
        payload = json.loads(stdin)
        directory = Path(payload["screenshot_dir"])
        directory.mkdir(parents=True)
        (directory / "desktop.png").write_bytes(b"desktop")
        (directory / "mobile.png").write_bytes(b"mobile")
        callback = payload.get("mutation_callback")
        is_report = str(command[-1]).endswith("pilot_acceptance_report.mjs")
        if callback:
            headers = {
                "Authorization": f"Bearer {callback['token']}",
                "Content-Type": "application/json",
            }
            if payload.get("action") == "r05":
                root = f"/api/v1/change-requests/{payload['change_request_id']}"
                operations = [
                    ("request-r05-approval", "POST", f"{root}/phase-approval/approve", 404, {}),
                    ("request-r05-execution", "POST", f"{root}/phase-execution/start", 404, {}),
                ]
            elif payload.get("action") == "v08_reinvestigate":
                operations = [(
                    "request-v08-reinvestigate", "POST",
                    f"/api/v1/incidents/{payload['incident_id']}/reinvestigate", 200,
                    {
                        "investigation.id": "investigation-run-two",
                        "investigation.sequence": 2,
                    },
                )]
            elif is_report:
                root = f"/api/v1/incidents/{payload['incident_id']}/report"
                operations = [
                    (
                        "request-v07-draft", "PATCH", root, 200,
                        {"draft.id": "report-draft-1", "draft.source_revision": 7},
                    ),
                    (
                        "request-v07-publish", "POST", f"{root}/publish", 201,
                        {
                            "publication.id": "report-publication-1",
                            "publication.source_revision": 7,
                            "publication.version": 1,
                        },
                    ),
                ]
            else:
                operations = [(
                    "console-user-1", "POST", "/api/v1/admin/users",
                    payload.get("mutation_status", 201),
                    {"user.id": "user-1", "user.revision": 1},
                )]
            for request_id, method, path, status, identities in operations:
                result = {
                    "request_id": request_id,
                    "status": status,
                    "response_request_id": request_id,
                    "identities": identities,
                }
                if payload.get("action") == "r05":
                    result["error_code"] = "not_found"
                for endpoint, body in (
                    ("intent", {"request_id": request_id, "method": method, "path": path}),
                    ("result", result),
                ):
                    request = urllib.request.Request(
                        f"{callback['url']}/{endpoint}",
                        data=json.dumps(body).encode(), headers=headers, method="POST",
                    )
                    with urllib.request.urlopen(request) as response:
                        assert response.status == 204
        extra = {}
        if payload.get("action") == "r05":
            root = f"/api/v1/change-requests/{payload['change_request_id']}"
            extra["denials"] = [
                {
                    "method": method, "path": path, "status": 404,
                    "request_id": request_id, "response_request_id": request_id,
                    "error_code": "not_found",
                    "payload_keys": ["error", "request_id", "service", "status"],
                    "error_keys": ["code", "message"],
                }
                for method, path, request_id in (
                    ("GET", f"{root}/phase-approval", "request-r05-review"),
                    ("POST", f"{root}/phase-approval/approve", "request-r05-approval"),
                    ("POST", f"{root}/phase-execution/start", "request-r05-execution"),
                )
            ]
        elif payload.get("action") == "v08_reinvestigate":
            extra.update({
                "action": "v08_reinvestigate",
                "investigation": {"id": "investigation-run-two", "sequence": 2},
            })
        elif is_report:
            extra["publication"] = {
                "id": "report-publication-1", "draft_id": "report-draft-1",
                "incident_id": payload["incident_id"], "source_revision": 7,
                "version": 1, "status": "published", "narrative": payload["narrative"],
            }
        return CommandResult(
            tuple(command),
            0,
            json.dumps(
                {
                    "same_origin": True,
                    "origins": ["http://192.0.2.10:30088"],
                    "paths": ["/", "/assets/app.js", "/auth/login", "/api/v1/actor"],
                    "browser_context": {
                        "role": "platform_administrator" if callback else payload.get("role", "authenticated"),
                        "persistent": False,
                        "storage_state_loaded": False,
                    },
                    "screenshots_masked": True,
                    **extra,
                }
            ),
            "",
            1,
        )


def test_browser_adapter_passes_password_only_over_stdin(tmp_path: Path) -> None:
    commands = BrowserCommands()
    result = PlaywrightBrowser(commands=commands, source_root=tmp_path).probe(
        "http://192.0.2.10:30088", username="admin", password="secret-password"
    )
    assert "secret-password" not in " ".join(commands.command)
    assert json.loads(commands.stdin)["password"] == "secret-password"
    assert set(result.screenshots) == {"desktop.png", "mobile.png"}
    assert result.summary["screenshots_masked"] is True
    assert result.summary["browser_context"]["persistent"] is False


def test_browser_adapter_uses_a_new_declared_role_context_per_probe(tmp_path: Path) -> None:
    commands = BrowserCommands()
    roles = []
    for role in ("platform_administrator", "no_approval_authority", "sre"):
        result = PlaywrightBrowser(commands=commands, source_root=tmp_path).probe(
            "http://192.0.2.10:30088", username=role, password="in-memory", role=role,
        )
        roles.append(result.summary["browser_context"])

    assert roles == [
        {"role": role, "persistent": False, "storage_state_loaded": False}
        for role in ("platform_administrator", "no_approval_authority", "sre")
    ]


def test_v01_console_adapter_keeps_both_passwords_on_stdin(tmp_path: Path) -> None:
    commands = BrowserCommands()
    ids = count(1)
    evidence = AcceptanceEvidence.create(
        tmp_path / "acceptance",
        acceptance_id="v0.1.0-browser-test",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        acceptance_tool_sha256="b" * 64,
        gate_contract_revision=GATE_CONTRACT_REVISION,
        kube_context="pilot-clean",
        cluster_identity_sha256="c" * 64,
        access_profile="http_nodeport",
        now=lambda: "2026-07-16T01:02:03Z",
        new_execution_id=lambda: f"execution-{next(ids)}",
    )
    for gate in GATE_SEQUENCE[: GATE_SEQUENCE.index("V01")]:
        started = evidence.start_gate(gate)
        evidence.record_gate(
            gate, "not_applicable" if gate == "I04" else "passed", [], started_at=started,
        )
    evidence.start_gate("V01")
    result = PlaywrightV01Console(
        commands=commands, source_root=tmp_path, evidence=evidence,
    ).provision_v01(
        base_url="http://192.0.2.10:30088",
        admin_username="admin",
        admin_password="admin-password",
        sre_username="pilot-sre",
        sre_password="sre-password",
    )
    command = " ".join(commands.command)
    assert "admin-password" not in command and "sre-password" not in command
    payload = json.loads(commands.stdin)
    assert payload["admin_password"] == "admin-password"
    assert payload["sre_password"] == "sre-password"
    assert result.summary["same_origin"] is True
    assert result.summary["screenshots_masked"] is True
    assert result.summary["browser_context"] == {
        "role": "platform_administrator", "persistent": False, "storage_state_loaded": False,
    }
    assert result.summary["mutations"] == [{
        "request_id": "console-user-1", "method": "POST", "path": "/api/v1/admin/users",
        "status": 201, "response_request_id": "console-user-1",
        "identities": {"user.id": "user-1", "user.revision": 1},
    }]
    execution = evidence.resume_gate("V01")
    assert next(
        item for item in execution.operations if item["kind"] == "console_mutation"
    )["operation_id"] == "console-user-1"
    assert execution.reconciliations[0]["public_fact"]["identities"] == {
        "user.id": "user-1", "user.revision": 1,
    }


def test_governed_change_adapter_uses_fresh_no_authority_browser_context(
    tmp_path: Path,
) -> None:
    commands = BrowserCommands()
    evidence = AcceptanceEvidence.create(
        tmp_path / "acceptance",
        acceptance_id="governed-browser-test",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        acceptance_tool_sha256="b" * 64,
        gate_contract_revision=GATE_CONTRACT_REVISION,
        kube_context="pilot-clean",
        cluster_identity_sha256="c" * 64,
        access_profile="http_nodeport",
    )
    for gate in GATE_SEQUENCE[: GATE_SEQUENCE.index("R05")]:
        started = evidence.start_gate(gate)
        evidence.record_gate(
            gate, "not_applicable" if gate == "I04" else "passed", [], started_at=started,
        )
    evidence.start_gate("R05")
    result = PlaywrightV01Console(
        commands=commands, source_root=tmp_path, evidence=evidence,
    ).verify_r05(
        base_url="http://192.0.2.10:30088",
        username="ordinary-user",
        password="ordinary-password",
        incident_id="incident-1",
        change_request_id="change-1",
        phase_id="phase-1",
        revision_id="revision-1",
        dry_run_hash="d" * 64,
        target_confirmation="apps/v1:Deployment:payments/checkout-api",
        run_id="run-1",
    )
    assert commands.command[-1].endswith("pilot_acceptance_governed_change.mjs")
    payload = json.loads(commands.stdin)
    assert payload["action"] == "r05"
    assert payload["phase_id"] == "phase-1"
    assert "ordinary-password" not in " ".join(commands.command)
    assert result.summary["same_origin"] is True
    assert [item["error_code"] for item in result.summary["mutations"]] == [
        "not_found", "not_found",
    ]


def test_v08_console_reinvestigation_binds_mutation_to_v08_ledger(tmp_path: Path) -> None:
    commands = BrowserCommands()
    evidence = AcceptanceEvidence.create(
        tmp_path / "acceptance", acceptance_id="rerun-browser-test",
        release_version="v0.1.0", release_sha256="a" * 64,
        acceptance_tool_sha256="b" * 64,
        gate_contract_revision=GATE_CONTRACT_REVISION,
        kube_context="pilot-clean", cluster_identity_sha256="c" * 64,
        access_profile="http_nodeport",
    )
    for gate in GATE_SEQUENCE[: GATE_SEQUENCE.index("V08")]:
        started = evidence.start_gate(gate)
        evidence.record_gate(
            gate, "not_applicable" if gate == "I04" else "passed", [], started_at=started,
        )
    evidence.start_gate("V08")

    result = PlaywrightV01Console(
        commands=commands, source_root=tmp_path, evidence=evidence,
    ).reinvestigate_v08(
        base_url="http://192.0.2.10:30088", username="pilot-sre",
        password="in-memory-password", incident_id="incident-run-one",
    )

    payload = json.loads(commands.stdin)
    assert payload["action"] == "v08_reinvestigate"
    assert "in-memory-password" not in " ".join(commands.command)
    assert result.summary["investigation"]["id"] == "investigation-run-two"
    assert result.summary["mutations"] == [{
        "request_id": "request-v08-reinvestigate", "method": "POST",
        "path": "/api/v1/incidents/incident-run-one/reinvestigate", "status": 200,
        "response_request_id": "request-v08-reinvestigate",
        "identities": {
            "investigation.id": "investigation-run-two",
            "investigation.sequence": 2,
        },
    }]


def test_report_adapter_publishes_only_through_a_fresh_console_context(
    tmp_path: Path,
) -> None:
    commands = BrowserCommands()
    evidence = AcceptanceEvidence.create(
        tmp_path / "acceptance",
        acceptance_id="report-browser-test",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        acceptance_tool_sha256="b" * 64,
        gate_contract_revision=GATE_CONTRACT_REVISION,
        kube_context="pilot-clean",
        cluster_identity_sha256="c" * 64,
        access_profile="http_nodeport",
    )
    for gate in GATE_SEQUENCE[: GATE_SEQUENCE.index("V07")]:
        started = evidence.start_gate(gate)
        evidence.record_gate(
            gate, "not_applicable" if gate == "I04" else "passed", [], started_at=started,
        )
    evidence.start_gate("V07")
    result = PlaywrightV01Console(
        commands=commands, source_root=tmp_path, evidence=evidence,
    ).publish_v07(
        base_url="http://192.0.2.10:30088",
        username="pilot-sre",
        password="sre-password",
        incident_id="incident-1",
        narrative={
            "impact": "impact", "root_cause": "root cause",
            "resolution_summary": "resolved", "follow_up": "follow up",
        },
    )
    payload = json.loads(commands.stdin)
    assert commands.command[-1].endswith("pilot_acceptance_report.mjs")
    assert payload["password"] == "sre-password"
    assert [item["path"] for item in result.summary["mutations"]] == [
        "/api/v1/incidents/incident-1/report",
        "/api/v1/incidents/incident-1/report/publish",
    ]
    assert result.summary["publication"]["id"] == "report-publication-1"


def test_subprocess_command_records_time_and_does_not_inherit_proxy(monkeypatch) -> None:
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "npm_config_proxy",
    ):
        monkeypatch.setenv(name, "http://proxy.example.test:8080")
    result = SubprocessCommands().run(["python3", "-c", "print('ok')"])
    assert result.exit_code == 0
    assert result.started_at and result.started_at.endswith("Z")
    assert result.completed_at and result.completed_at.endswith("Z")
    environment = SubprocessCommands().run(
        [
            "python3",
            "-c",
            "import os; print('|'.join(os.environ.get(name, '') for name in "
            "('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'npm_config_proxy')))",
        ]
    )
    assert environment.stdout.strip() == "||||"


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="OpenSSH unavailable")
def test_openssh_attestation_signature_round_trip(tmp_path: Path) -> None:
    key = tmp_path / "signing-key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True
    )
    statement = {
        "acceptance_id": "v0.1.0-test",
        "actor": "operator@example.test",
        "gate_ids": ["P03"],
        "conclusion": "passed",
    }
    signer = OpenSshSigner()
    signed = signer.sign(statement, key_path=key)
    signer.verify(
        statement,
        signature=signed["signature"],
        public_key=signed["public_key"],
        identity=statement["actor"],
    )
    assert signed["fingerprint"].startswith("SHA256:")
    assert "PRIVATE" not in signed["signature"]


class TelemetryCommands:
    def __init__(self) -> None:
        self.deployments = []
        self.responses = [
            {"data": {"activeTargets": [{"labels": {"job": "aiops-gateway"}, "health": "up"}, {"labels": {"job": "kube-state-metrics"}, "health": "up"}]}},
            {"data": {"groups": [{"rules": [{"name": "AIOpsOwnerDown", "health": "ok"}]}]}},
            {"data": {"result": [{"metric": {"__name__": "aiops_gateway_sse_connections", "namespace": "aiops-system"}, "value": [1, "0"]}]}},
            {"data": {"result": [{"stream": {"namespace": "aiops-system"}, "values": [["1", "raw log must not persist"]]}]}},
            {"config": {"original": 'receiver: gateway\n- aiops_route="gateway"'}},
            {"status": "succeeded", "data": {"returned_series": 1, "series": ["raw"]}, "evidence_refs": [{"source": "prometheus"}]},
            {"status": "succeeded", "data": {"returned_lines": 1, "lines": ["raw log"]}, "evidence_refs": [{"source": "loki"}]},
        ]

    def run(self, command, **_kwargs):
        assert "token=$(cat" not in " ".join(command[:-1])
        self.deployments.append(command[4])
        return CommandResult(tuple(command), 0, json.dumps(self.responses.pop(0)), "", 0.1)


def test_telemetry_probe_retains_only_counts_and_hashes() -> None:
    commands = TelemetryCommands()
    summary = KubernetesTelemetryProbe(commands, now=lambda: 1_700_000_000).probe()
    serialized = json.dumps(summary)
    assert summary["workload_series"] == summary["loki_streams"] == 1
    assert summary["mcp_prometheus"]["returned_series"] == 1
    assert summary["mcp_loki"]["returned_lines"] == 1
    assert "raw log" not in serialized and '"series": ["raw"]' not in serialized
    assert commands.deployments == [
        "deployment/aiops-mcp-prometheus",
        "deployment/aiops-mcp-prometheus",
        "deployment/aiops-mcp-prometheus",
        "deployment/aiops-mcp-loki",
        "deployment/aiops-alertmanager",
        "deployment/aiops-diagnosis",
        "deployment/aiops-diagnosis",
    ]


@pytest.mark.parametrize(
    ("alertmanager_severity", "matches"),
    [("critical", True), ("warning", False)],
)
def test_run_signal_probe_links_exact_run_without_persisting_log_lines(
    alertmanager_severity: str, matches: bool
) -> None:
    class Commands:
        def __init__(self) -> None:
            self.responses = [
                {"data": {"result": [{"metric": {"run_id": "run-1"}, "value": [1, "1"]}]}},
                {"data": {"result": [{"metric": {"deployment": "verification-api"}, "value": [1, "1"]}]}},
                {"data": {"alerts": [{
                    "labels": {
                        "alertname": "AIOpsVerificationWorkloadUnavailable",
                        "namespace": "aiops-verification",
                        "deployment": "verification-api",
                        "run_id": "run-1",
                        "severity": "critical",
                    },
                    "state": "firing",
                }]}},
                {"data": {"result": [{
                    "stream": {"namespace": "aiops-verification"},
                    "values": [["1", '{"event":"verification_fault_activated","run_id":"run-1"}']],
                }]}},
                [{
                    "labels": {
                        "alertname": "AIOpsVerificationWorkloadUnavailable",
                        "namespace": "aiops-verification",
                        "deployment": "verification-api",
                        "run_id": "run-1",
                        "severity": alertmanager_severity,
                    },
                    "status": {"state": "active"},
                    "fingerprint": "fingerprint-run-1",
                }],
            ]

        def run(self, command, **_kwargs):
            return CommandResult(
                tuple(command), 0, json.dumps(self.responses.pop(0)), "", 0.1,
            )

    summary = KubernetesTelemetryProbe(Commands(), now=lambda: 1_700_000_000).probe_v02("run-1")

    assert summary["fault_metric_series"] == 1
    assert summary["deployment_unavailable_series"] == 1
    assert summary["activation_log_lines"] == 1
    labels_sha256 = summary["prometheus_alerts"][0]["labels_sha256"]
    assert summary["prometheus_alerts"] == [
        {"labels_sha256": labels_sha256, "state": "firing"},
    ]
    assert summary["alertmanager_alerts"] == ([{
        "fingerprint": "fingerprint-run-1",
        "labels_sha256": labels_sha256,
        "status": "active",
    }] if matches else [])
    assert "verification_fault_activated" not in json.dumps(summary)


def test_recovery_probe_links_zero_metric_log_and_cleared_alert() -> None:
    class Commands:
        def __init__(self) -> None:
            self.responses = [
                {"data": {"result": [{
                    "metric": {
                        "namespace": "aiops-verification", "deployment": "verification-api",
                        "service": "verification-api", "run_id": "run-1",
                    },
                    "value": [1_700_000_000, "0"],
                }]}},
                {"data": {"alerts": []}},
                {"data": {"result": [{
                    "stream": {"namespace": "aiops-verification", "container": "verification-api"},
                    "values": [["1700000000000000000", '{"event":"verification_fault_recovered","run_id":"run-1"}']],
                }]}},
                [],
            ]

        def run(self, command, **_kwargs):
            return CommandResult(
                tuple(command), 0, json.dumps(self.responses.pop(0)), "", 0.1,
            )

    summary = KubernetesTelemetryProbe(Commands(), now=lambda: 1_700_000_000).probe_v06(
        "run-1", "fingerprint-run-1",
    )

    assert summary["recovery_metric_series"] == 1
    assert summary["recovery_metric_zero"] is True
    observed_at = summary["recovery_metric_observed_at"], summary["recovery_log_observed_at"]
    assert observed_at == (1_700_000_000, 1_700_000_000)
    assert summary["recovery_log_lines"] == 1
    assert summary["prometheus_alert_firing"] is False
    assert summary["alertmanager_alert_active"] is False
    assert "verification_fault_recovered" not in json.dumps(summary)


def test_https_profile_probe_retains_ingress_and_certificate_identity(monkeypatch) -> None:
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def getpeercert(self, binary_form=False):
            if binary_form:
                return b"certificate"
            return {
                "subject": ((('commonName', 'console.example.test'),),),
                "issuer": ((('commonName', 'Test CA'),),),
                "notAfter": "Jan  1 00:00:00 2030 GMT",
            }

        def version(self):
            return "TLSv1.3"

    class Context:
        def wrap_socket(self, _raw, server_hostname=None):
            assert server_hostname == "console.example.test"
            return Connection()

    class Commands:
        def run(self, command, **_kwargs):
            resource = {
                "metadata": {"uid": "ingress-uid", "generation": 2},
                "spec": {
                    "ingressClassName": "nginx",
                    "rules": [{"host": "console.example.test"}],
                    "tls": [{"hosts": ["console.example.test"], "secretName": "console-tls"}],
                },
            }
            return CommandResult(tuple(command), 0, json.dumps(resource), "", 0.1)

    monkeypatch.setattr(adapters.socket, "create_connection", lambda *_args, **_kwargs: Connection())
    monkeypatch.setattr(adapters.ssl, "create_default_context", lambda: Context())
    identity = HttpsProfileProbe(Commands()).probe(
        "https://console.example.test", ingress="edge/aiops"
    )
    assert identity["ingress"]["uid"] == "ingress-uid"
    assert identity["tls"]["protocol"] == "TLSv1.3"
    assert len(identity["tls"]["certificate_sha256"]) == 64
