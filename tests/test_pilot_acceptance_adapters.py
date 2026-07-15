from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from aiops.acceptance import adapters
from aiops.acceptance.adapters import HttpsProfileProbe, OpenSshSigner, PlaywrightBrowser
from aiops.acceptance.command import CommandResult, SubprocessCommands
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
        return CommandResult(
            tuple(command),
            0,
            json.dumps(
                {
                    "same_origin": True,
                    "origins": ["http://192.0.2.10:30088"],
                    "paths": ["/", "/assets/app.js", "/auth/login", "/api/v1/actor"],
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
