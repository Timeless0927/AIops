from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest

from aiops.acceptance.adapters import PlaywrightV01Console
from aiops.acceptance.command import CommandResult
from tests.pilot_acceptance_recovery_support import recovery_ledger


class R03BrowserCommands:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str]] = []

    def run(self, command, *, stdin=None, **_kwargs) -> CommandResult:
        assert isinstance(stdin, str)
        value = tuple(command)
        self.calls.append((value, stdin))
        payload = json.loads(stdin)
        action = payload["action"]
        screenshot_dir = Path(payload["screenshot_dir"])
        screenshot_dir.mkdir(parents=True)
        (screenshot_dir / f"{action}.png").write_bytes(b"masked")
        callback = payload["mutation_callback"]
        if action == "r03_prepare":
            operations = [(
                "r03-prepare-request", "POST",
                f"/api/v1/incidents/{payload['incident_id']}/change-requests",
                201,
                {
                    "change_request.id": "change-probe",
                    "change_request.active_revision.id": "revision-probe",
                },
                None,
            )]
            result = {"prepared": {
                "change_request_id": "change-probe",
                "phase_id": "phase-probe",
                "revision_id": "revision-probe",
                "dry_run_hash": "d" * 64,
                "target_confirmation": (
                    "apps/v1:Deployment:aiops-verification/verification-api"
                ),
                "approval_status": "awaiting_approval",
                "change_summary": {
                    "target": {
                        "api_version": "apps/v1", "kind": "Deployment",
                        "namespace": "aiops-verification", "name": "verification-api",
                    },
                    "operation": "patch",
                    "diff": [{
                        "op": "add",
                        "path": "/metadata/annotations/aiops.dev~1r03-grant-probe",
                        "after": payload["operation_id"],
                    }],
                    "post_checks": [{"type": "json_pointer"}],
                    "rollback": {"status": "available"},
                },
            }}
        elif action == "r03_admin":
            denial = self._denial("r03-admin-request")
            operations = [(
                denial["request_id"], "POST", "/api/v1/admin/connector-commands",
                409, {}, "cluster_not_ready",
            )]
            result = {"live_evidence": denial}
        elif action == "r03_sre":
            root = f"/api/v1/change-requests/{payload['prepared']['change_request_id']}"
            dry_run = self._denial("r03-dry-run-request")
            grant = self._denial("r03-grant-request")
            approval = {
                "request_id": "r03-approval-request", "status": 201,
                "response_request_id": "r03-approval-request",
            }
            operations = [
                (
                    dry_run["request_id"], "POST",
                    f"/api/v1/incidents/{payload['incident_id']}/change-requests",
                    409, {}, "cluster_not_ready",
                ),
                (
                    approval["request_id"], "POST", f"{root}/phase-approval/approve",
                    201,
                    {"approval.id": "approval-probe", "phase_review.revision_id": "revision-probe"},
                    None,
                ),
                (
                    grant["request_id"], "POST", f"{root}/phase-execution/start",
                    409, {}, "cluster_not_ready",
                ),
            ]
            result = {
                "denials": {"dry_run": dry_run, "grant": grant},
                "approval": approval,
                "active_commands": 0,
                "grants_created": 0,
            }
        else:
            raise AssertionError(f"unexpected action {action}")
        for request_id, method, path, status, identities, error_code in operations:
            self._callback(callback, "intent", {
                "request_id": request_id, "method": method, "path": path,
            })
            terminal = {
                "request_id": request_id, "status": status,
                "response_request_id": request_id, "identities": identities,
            }
            if error_code is not None:
                terminal["error_code"] = error_code
            self._callback(callback, "result", terminal)
        summary = {
            "action": action,
            "same_origin": True,
            "screenshots_masked": True,
            **result,
        }
        return CommandResult(value, 0, json.dumps(summary), "", 0.1)

    @staticmethod
    def _denial(request_id: str) -> dict[str, object]:
        return {
            "request_id": request_id, "status": 409,
            "response_request_id": request_id,
            "error_code": "cluster_not_ready",
        }

    @staticmethod
    def _callback(callback: dict[str, str], kind: str, body: dict[str, object]) -> None:
        request = urllib.request.Request(
            f"{callback['url']}/{kind}",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {callback['token']}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            assert response.status == 204


def _console(tmp_path: Path, commands) -> PlaywrightV01Console:
    evidence = recovery_ledger(tmp_path / "ledger", "R03")
    evidence.start_gate("R03")
    return PlaywrightV01Console(
        commands=commands, source_root=tmp_path, evidence=evidence,
    )


def test_r03_browser_adapters_bind_exact_console_mutations_and_keep_passwords_on_stdin(
    tmp_path: Path,
) -> None:
    commands = R03BrowserCommands()
    console = _console(tmp_path, commands)
    prepared = console.prepare_r03(
        base_url="https://aiops.example",
        username="pilot-sre",
        password="sre-password",
        incident_id="incident-run-one",
        connector_id="connector-prod",
        cluster_id="pilot-cluster",
        operation_id="r03/execution/prepare",
    )
    admin = console.verify_r03_admin_denial(
        base_url="https://aiops.example",
        username="admin",
        password="admin-password",
        cluster_id="pilot-cluster",
        operation_id="r03/execution/deny",
    )
    sre = console.verify_r03_sre_denials(
        base_url="https://aiops.example",
        username="pilot-sre",
        password="sre-password",
        incident_id="incident-run-one",
        prepared=prepared.summary["prepared"],
        operation_id="r03/execution/deny",
    )

    payloads = [json.loads(stdin) for _command, stdin in commands.calls]
    assert [payload["action"] for payload in payloads] == [
        "r03_prepare", "r03_admin", "r03_sre",
    ]
    assert [payload["password"] for payload in payloads] == [
        "sre-password", "admin-password", "sre-password",
    ]
    assert all(
        "password" not in " ".join(command) for command, _stdin in commands.calls
    )
    assert set(prepared.screenshots) == {"r03_prepare.png"}
    assert set(admin.screenshots) == {"r03_admin.png"}
    assert set(sre.screenshots) == {"r03_sre.png"}
    assert [item["status"] for item in sre.summary["mutations"]] == [409, 201, 409]
    assert sre.summary["denials"]["grant"]["error_code"] == "cluster_not_ready"


def test_r03_browser_adapter_redacts_callback_token_from_command_failure(
    tmp_path: Path,
) -> None:
    class FailingCommands:
        def run(self, command, *, stdin=None, **_kwargs) -> CommandResult:
            payload = json.loads(stdin)
            token = payload["mutation_callback"]["token"]
            return CommandResult(tuple(command), 1, "", f"callback failed: {token}", 0.1)

    console = _console(tmp_path, FailingCommands())
    with pytest.raises(RuntimeError) as failure:
        console.verify_r03_admin_denial(
            base_url="https://aiops.example",
            username="admin",
            password="admin-password",
            cluster_id="pilot-cluster",
            operation_id="r03/execution/deny",
        )
    assert "callback failed: [REDACTED]" in str(failure.value)
