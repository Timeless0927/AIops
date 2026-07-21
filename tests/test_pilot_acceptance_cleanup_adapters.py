from __future__ import annotations

import json
from pathlib import Path

from aiops.acceptance.cleanup import CleanupHistoryScope, CleanupScope
from aiops.acceptance.cleanup_adapters import GatewayCleanupHistoryAdapter, KubectlCleanupAdapter
from aiops.acceptance.command import CommandResult
from aiops.acceptance.http import HttpResponse


class Commands:
    def __init__(self) -> None:
        self.run_deleted = False
        self.base_deleted = False
        self.calls: list[tuple[str, ...]] = []

    def run(self, command, **_kwargs):
        call = tuple(command)
        self.calls.append(call)
        if "delete" in call:
            target = call[call.index("-k") + 1]
            if target.endswith("verification/run"):
                self.run_deleted = True
            if target.endswith("verification/base"):
                self.base_deleted = True
            return CommandResult(call, 0, "", "", 1)
        if "deployment,daemonset,statefulset,service,pvc,configmap" in call:
            return CommandResult(call, 0, json.dumps({"items": [{
                "kind": "Deployment",
                "metadata": {"name": "aiops-gateway", "uid": "gateway-uid"},
            }]}), "", 1)
        name = call[call.index("namespace") + 1] if "namespace" in call else "verification-trigger"
        if name == "aiops-system":
            value = {"metadata": {"name": name, "uid": "system-uid"}}
        elif name == "aiops-verification":
            value = None if self.base_deleted else {"metadata": {"name": name, "uid": "fixture-uid"}}
        else:
            value = None if self.run_deleted or self.base_deleted else {
                "metadata": {
                    "name": name, "namespace": "aiops-verification",
                    "uid": "run-controller-uid-2",
                },
            }
        return CommandResult(call, 0, json.dumps(value) if value else "", "", 1)


def test_kubectl_cleanup_adapter_uses_only_fixed_fixture_paths(tmp_path: Path) -> None:
    release_root = tmp_path / "release"
    (release_root / "verification/run").mkdir(parents=True)
    (release_root / "verification/base").mkdir(parents=True)
    for path in (release_root / "verification/run", release_root / "verification/base"):
        (path / "kustomization.yaml").write_text("resources: []\n")
    commands = Commands()
    adapter = KubectlCleanupAdapter(commands, kube_context="pilot-context")
    scope = CleanupScope(release_root=release_root, expected_run_id="run-controller-uid-2")
    before = adapter.snapshot(scope)

    after_run = adapter.delete_run(scope, before, operation_id="c01/run")
    after_base = adapter.delete_base(scope, before, operation_id="c01/base")

    deletes = [call for call in commands.calls if "delete" in call]
    assert deletes == [
        (
            "kubectl", "--context", "pilot-context", "delete", "-k",
            str(release_root / "verification/run"), "--ignore-not-found", "--wait=true",
        ),
        (
            "kubectl", "--context", "pilot-context", "delete", "-k",
            str(release_root / "verification/base"), "--ignore-not-found", "--wait=true",
        ),
    ]
    assert all("aiops-system" not in call for call in deletes)
    assert after_run["run_job"] is None
    assert after_base["fixture_namespace"] is None
    assert after_base["system_namespace"] == before["system_namespace"]


def _history_scope() -> CleanupHistoryScope:
    report_v1 = {"id": "report-v1", "version": 1, "status": "published"}
    report_v2 = {
        "id": "report-v2", "version": 2, "status": "published",
        "facts": {
            "incident": {"id": "incident-1", "deployment_target_id": "target-1"},
            "investigations": [{"id": "investigation-1"}, {"id": "investigation-2"}],
            "evidence_steps": [{"id": "evidence-1"}, {"id": "evidence-2"}],
            "recovery_observations": [{"id": "recovery-1"}, {"id": "recovery-2"}],
        },
        "decision_action_history": {
            "recommended_actions": [{"id": "action-1"}, {"id": "action-2"}],
            "change_requests": [
                {"id": "change-1", "phases": [{
                    "id": "phase-1", "revisions": [{"id": "revision-1"}],
                    "approval": {"id": "approval-1"},
                    "execution": {"id": "execution-1", "steps": [{"command_id": "command-1"}]},
                }]},
                {"id": "change-2", "phases": [{
                    "id": "phase-2", "revisions": [{"id": "revision-2"}],
                    "approval": {"id": "approval-2"},
                    "execution": {"id": "execution-2", "steps": [{"command_id": "command-2"}]},
                }]},
            ],
        },
    }
    inventory = {
        "investigation_ids": ["investigation-1", "investigation-2"],
        "evidence_step_ids": ["evidence-1", "evidence-2"],
        "recommended_action_ids": ["action-1", "action-2"],
        "change_request_ids": ["change-1", "change-2"],
        "phase_ids": ["phase-1", "phase-2"], "revision_ids": ["revision-1", "revision-2"],
        "approval_ids": ["approval-1", "approval-2"], "grant_ids": ["grant-1", "grant-2"],
        "command_ids": ["command-1", "command-2"],
        "execution_ids": ["execution-1", "execution-2"],
        "recovery_observation_ids": ["recovery-1", "recovery-2"],
        "report_ids": ["report-v1", "report-v2"],
        "delivery_ids": ["delivery-1", "delivery-2"],
    }
    deliveries = (
        {"id": "delivery-1", "status": "sent"},
        {"id": "delivery-2", "status": "sent"},
    )
    return CleanupHistoryScope(
        incident_id="incident-1", identity_inventory=inventory,
        report_v1=report_v1, report_v2=report_v2,
        notification_deliveries=deliveries, deployment_target_id="target-1",
    )


def test_gateway_cleanup_history_reads_only_actor_scoped_public_projections() -> None:
    scope = _history_scope()
    calls: list[tuple[str, str]] = []

    class User:
        def request(self, method: str, path: str):
            calls.append((method, path))
            if path.endswith("/workbench"):
                return HttpResponse(200, {
                    "incident": {"id": "incident-1"},
                    "investigation": {"id": "investigation-2"},
                }, {})
            if path.endswith("/report"):
                return HttpResponse(200, {
                    "publications": [scope.report_v2, scope.report_v1],
                }, {})
            if path == "/api/v1/resources":
                return HttpResponse(200, {"resources": [{
                    "id": "target-1", "binding_state": "bound",
                    "availability": "unavailable",
                }]}, {})
            change_id = path.split("/")[4]
            suffix = change_id[-1]
            return HttpResponse(200, {"phase_execution": {
                "id": f"execution-{suffix}", "status": "succeeded",
                "grant": {"id": f"grant-{suffix}"},
                "steps": [{"command_id": f"command-{suffix}"}],
            }}, {})

    class Admin:
        def request(self, method: str, path: str):
            calls.append((method, path))
            return HttpResponse(200, {
                "deliveries": list(scope.notification_deliveries),
            }, {})

    result = GatewayCleanupHistoryAdapter(
        user=User(), notification_admin=Admin(), attempts=1,
        sleep=lambda _seconds: None,
    ).read(scope)

    assert result["identity_inventory"] == scope.identity_inventory
    assert result["resource"]["availability"] == "unavailable"
    assert all(method == "GET" for method, _path in calls)
