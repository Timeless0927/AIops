from __future__ import annotations

import json
import urllib.request
from copy import deepcopy
from pathlib import Path

import pytest

from aiops.acceptance.adapters import PlaywrightV01Console
from aiops.acceptance.command import CommandResult
from aiops.acceptance.recovery import load_recovery_scope
from aiops.acceptance.stale_change import ANNOTATION_PATH, TARGET
from aiops.acceptance.stale_change_adapters import KubectlStaleChangeAdapter
from tests.pilot_acceptance_recovery_support import recovery_ledger


class KubectlCommands:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.resource = {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {
                "namespace": "aiops-verification", "name": "verification-api",
                "uid": "verification-uid", "resourceVersion": "10", "annotations": {},
            },
            "spec": {"template": {"metadata": {"labels": {"app": "verification-api"}}}},
        }

    def run(self, command, **_kwargs) -> CommandResult:
        value = tuple(command)
        self.commands.append(value)
        args = value[5:]
        if args[:3] == ("get", "deployment", "verification-api"):
            return CommandResult(value, 0, json.dumps(self.resource), "", 0.1)
        if args[:3] == ("patch", "deployment", "verification-api"):
            payload = json.loads(args[args.index("-p") + 1])
            assert set(payload) == {"metadata"}
            assert payload["metadata"]["resourceVersion"] == "10"
            self.resource["metadata"]["annotations"].update(payload["metadata"]["annotations"])
            self.resource["metadata"]["resourceVersion"] = "11"
            return CommandResult(value, 0, json.dumps(self.resource), "", 0.1)
        raise AssertionError(value)


def _kubectl_adapter(tmp_path: Path, commands: KubectlCommands):
    scope = load_recovery_scope(recovery_ledger(tmp_path, "R06"))
    adapter = KubectlStaleChangeAdapter(
        commands,
        kube_context=scope.kube_context,
        candidate_sha256=scope.candidate_sha256,
        release_inventory_sha256=scope.release_inventory_sha256,
        cluster_identity_sha256=scope.cluster_identity_sha256,
    )
    return scope, adapter


def test_r06_kubectl_adapter_changes_only_top_level_metadata_and_reconciles(
    tmp_path: Path,
) -> None:
    commands = KubectlCommands()
    scope, adapter = _kubectl_adapter(tmp_path, commands)
    before = adapter.snapshot(scope)
    template = deepcopy(commands.resource["spec"]["template"])

    drift = adapter.drift_metadata(
        scope, before, operation_id="r06/execution/drift",
    )
    reconciled = adapter.reconcile_metadata_drift(
        scope, before, operation_id="r06/execution/drift",
    )

    assert drift == reconciled
    assert drift["annotation_path"] == ANNOTATION_PATH
    assert drift["before"]["resource_version"] == "10"
    assert drift["after"]["resource_version"] == "11"
    assert commands.resource["spec"]["template"] == template
    assert sum(command[5] == "patch" for command in commands.commands) == 1


def test_r06_kubectl_adapter_rejects_wrong_target_before_patch(tmp_path: Path) -> None:
    commands = KubectlCommands()
    scope, adapter = _kubectl_adapter(tmp_path, commands)
    before = adapter.snapshot(scope)
    before["target"] = {**before["target"], "name": "wrong-target"}

    with pytest.raises(ValueError, match="fixed Operator drift"):
        adapter.drift_metadata(scope, before, operation_id="r06/execution/drift")
    assert all(command[5] != "patch" for command in commands.commands)


class BrowserCommands:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str]] = []

    def run(self, command, *, stdin=None, **_kwargs) -> CommandResult:
        assert isinstance(stdin, str)
        value = tuple(command)
        payload = json.loads(stdin)
        self.calls.append((value, stdin))
        action = payload["action"]
        directory = Path(payload["screenshot_dir"])
        directory.mkdir(parents=True)
        (directory / f"{action}.png").write_bytes(b"masked")
        callback = payload["mutation_callback"]
        if action == "r06_prepare":
            operations = [(
                "r06-create", "POST",
                f"/api/v1/incidents/{payload['incident_id']}/change-requests",
                {
                    "change_request.id": "change-r06",
                    "change_request.active_revision.id": "revision-r06",
                },
            )]
            summary = {"prepared": {
                "incident_id": payload["incident_id"],
                "change_request_id": "change-r06", "phase_id": "phase-r06",
                "revision_id": "revision-r06", "dry_run_hash": "d" * 64,
                "target_confirmation": "apps/v1:Deployment:aiops-verification/verification-api",
                "approval_status": "awaiting_approval",
                "target_identity": {"uid": "verification-uid", "resource_version": "10"},
                "change_summary": {
                    "target": TARGET, "operation": "patch",
                    "diff": [{
                        "op": "add", "path": ANNOTATION_PATH,
                        "after": payload["context"].split("r06-stale-probe=", 1)[1].split(". ", 1)[0],
                    }],
                    "post_checks": [{"type": "json_pointer", "path": ANNOTATION_PATH}],
                    "rollback": {"status": "available"},
                },
            }}
        elif action == "r06_approve":
            operations = [(
                "r06-approve", "POST",
                "/api/v1/change-requests/change-r06/phase-approval/approve",
                {"phase_review.approval.id": "approval-r06", "phase_review.revision_id": "revision-r06"},
            )]
            summary = {"phase_review": {
                "phase_id": "phase-r06", "revision_id": "revision-r06", "status": "approved",
                "approval": {
                    "id": "approval-r06", "rollback_policy": "stop_only",
                    "authority_ids": ["authority-r06"],
                    "frozen_changes": [{
                        "dry_run_hash": "d" * 64,
                        "target_confirmation": payload["target_confirmation"],
                    }],
                },
            }}
        elif action == "r06_start":
            operations = [(
                "r06-start", "POST",
                "/api/v1/change-requests/change-r06/phase-execution/start",
                {"phase_execution.id": "execution-r06", "phase_execution.revision_id": "revision-r06"},
            )]
            summary = {"phase_execution": {
                "id": "execution-r06", "change_request_id": "change-r06",
                "phase_id": "phase-r06", "revision_id": "revision-r06",
            }}
        else:
            raise AssertionError(action)
        for request_id, method, path, identities in operations:
            self._callback(callback, "intent", {
                "request_id": request_id, "method": method, "path": path,
            })
            self._callback(callback, "result", {
                "request_id": request_id, "status": 201,
                "response_request_id": request_id, "identities": identities,
            })
        return CommandResult(value, 0, json.dumps({
            "action": action, "same_origin": True,
            "origins": ["https://aiops.example"], "screenshots_masked": True,
            **summary,
        }), "", 0.1)

    @staticmethod
    def _callback(callback: dict[str, str], kind: str, payload: dict[str, object]) -> None:
        request = urllib.request.Request(
            f"{callback['url']}/{kind}", data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {callback['token']}",
                "Content-Type": "application/json",
            }, method="POST",
        )
        with urllib.request.urlopen(request) as response:
            assert response.status == 204


def test_r06_browser_adapters_split_prepare_approval_and_start_with_stdin_secrets(
    tmp_path: Path,
) -> None:
    evidence = recovery_ledger(tmp_path / "ledger", "R06")
    evidence.start_gate("R06")
    commands = BrowserCommands()
    console = PlaywrightV01Console(commands=commands, source_root=tmp_path, evidence=evidence)
    prepared = console.prepare_r06(
        base_url="https://aiops.example", username="sre", password="sre-password",
        incident_id="incident-run-one", cluster_id="pilot-cluster",
        operation_id="r06/execution/prepare",
        approved_value="user-approved:r06/execution/prepare",
    )
    approved = console.approve_r06(
        base_url="https://aiops.example", username="sre", password="sre-password",
        incident_id="incident-run-one", prepared=prepared.summary["prepared"],
        operation_id="r06/execution/approve",
    )
    started = console.start_r06(
        base_url="https://aiops.example", username="sre", password="sre-password",
        incident_id="incident-run-one", prepared=prepared.summary["prepared"],
        operation_id="r06/execution/start",
    )

    payloads = [json.loads(stdin) for _command, stdin in commands.calls]
    assert [item["action"] for item in payloads] == [
        "r06_prepare", "r06_approve", "r06_start",
    ]
    assert all(item["password"] == "sre-password" for item in payloads)
    assert all("sre-password" not in " ".join(command) for command, _ in commands.calls)
    assert [
        item.summary["mutations"][0]["path"]
        for item in (prepared, approved, started)
    ] == [
        "/api/v1/incidents/incident-run-one/change-requests",
        "/api/v1/change-requests/change-r06/phase-approval/approve",
        "/api/v1/change-requests/change-r06/phase-execution/start",
    ]
    assert [set(item.screenshots) for item in (prepared, approved, started)] == [
        {"r06_prepare.png"}, {"r06_approve.png"}, {"r06_start.png"},
    ]
