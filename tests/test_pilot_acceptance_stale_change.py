from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from aiops.acceptance.evidence import GateFailed
from aiops.acceptance.http import HttpResponse
from aiops.acceptance.recovery import RecoveryScope
from aiops.acceptance.stale_change import (
    ANNOTATION_PATH,
    TARGET,
    StaleChangeGateRunner,
)
from aiops.acceptance.web_gates import BrowserResult
from tests.pilot_acceptance_recovery_support import (
    attest_recovery_approval,
    attest_recovery_drift,
    recovery_ledger,
)


def _change(operation_id: str, approved_value: str) -> dict[str, object]:
    return {
        "dry_run_hash": "d" * 64,
        "target_confirmation": "apps/v1:Deployment:aiops-verification/verification-api",
        "canonical_change": {
            "target": {**TARGET, "uid": "verification-uid", "resource_version": "10"},
            "operation": "patch",
        },
        "diff": [{"op": "add", "path": ANNOTATION_PATH, "after": approved_value}],
        "post_checks": [{
            "type": "json_pointer", "path": ANNOTATION_PATH,
            "operator": "eq", "value": approved_value,
        }],
        "rollback": {"status": "available"},
        "operation_id": operation_id,
    }


def _prepared(operation_id: str, approved_value: str) -> dict[str, object]:
    change = _change(operation_id, approved_value)
    return {
        "incident_id": "incident-run-one",
        "change_request_id": "change-r06",
        "phase_id": "phase-r06",
        "revision_id": "revision-r06",
        "dry_run_hash": change["dry_run_hash"],
        "target_confirmation": change["target_confirmation"],
        "approval_status": "awaiting_approval",
        "target_identity": {"uid": "verification-uid", "resource_version": "10"},
        "change_summary": {
            "target": TARGET,
            "operation": "patch",
            "diff": change["diff"],
            "post_checks": change["post_checks"],
            "rollback": change["rollback"],
        },
    }


def _review(operation_id: str, approved_value: str, *, approved: bool) -> dict[str, object]:
    change = _change(operation_id, approved_value)
    approval = {
        "id": "approval-r06",
        "rollback_policy": "stop_only",
        "authority_ids": ["authority-r06"],
        "frozen_changes": [{
            "dry_run_hash": change["dry_run_hash"],
            "target_confirmation": change["target_confirmation"],
        }],
    } if approved else None
    return {
        "phase_id": "phase-r06", "revision_id": "revision-r06",
        "status": "approved" if approved else "awaiting_approval",
        "changes": [change], "approval": approval,
    }


def _executions() -> tuple[dict[str, object], dict[str, object]]:
    grant = {
        "id": "grant-r06", "issued_at": 1.0, "expires_at": 61.0,
        "consumed_at": None, "revoked_at": None,
    }
    step = {
        "id": "execution-r06:forward:1", "direction": "forward",
        "command_id": "command-r06", "status": "queued", "result": None,
    }
    initial = {
        "id": "execution-r06", "change_request_id": "change-r06",
        "phase_id": "phase-r06", "revision_id": "revision-r06",
        "approval_id": "approval-r06", "command_id": "command-r06",
        "status": "queued", "rollback_policy": "stop_only",
        "completed_at": None, "reconciliation": None,
        "grant": grant, "steps": [step],
    }
    terminal = deepcopy(initial)
    terminal.update({"status": "stale", "completed_at": 10.0})
    terminal["grant"] = {**grant, "consumed_at": 2.0}
    terminal["steps"] = [{
        **step,
        "status": "stale",
        "result": {
            "status": "rejected", "error_code": "stale_change",
            "error_message": "frozen target identity or resourceVersion changed",
        },
    }]
    return initial, terminal


class State:
    def __init__(self, interrupt_action: str | None = None) -> None:
        self.calls: list[str] = []
        self.interrupt_action = interrupt_action
        self.interrupted = False
        self.context: str | None = None
        self.operation_id: str | None = None
        self.approved_value: str | None = None
        self.review: dict[str, object] | None = None
        self.execution: dict[str, object] | None = None

    def interrupt(self, action: str) -> None:
        if self.interrupt_action == action and not self.interrupted:
            self.interrupted = True
            raise KeyboardInterrupt


class Console:
    def __init__(self, state: State) -> None:
        self.state = state

    def prepare_r06(self, **kwargs) -> BrowserResult:
        self.state.calls.append("prepare")
        self.state.operation_id = kwargs["operation_id"]
        self.state.approved_value = kwargs["approved_value"]
        self.state.context = (
            "Prepare only; do not execute. Patch only top-level annotation "
            f"aiops.dev/r06-stale-probe={kwargs['approved_value']}. "
            f"cluster_id={kwargs['cluster_id']} operation_id={kwargs['operation_id']}"
        )
        self.state.review = _review(
            kwargs["operation_id"], kwargs["approved_value"], approved=False,
        )
        result = _browser(
            "r06_prepare", prepared=_prepared(kwargs["operation_id"], kwargs["approved_value"]),
        )
        self.state.interrupt("prepare")
        return result

    def approve_r06(self, **_kwargs) -> BrowserResult:
        self.state.calls.append("approve")
        assert self.state.operation_id and self.state.approved_value
        self.state.review = _review(
            self.state.operation_id, self.state.approved_value, approved=True,
        )
        result = _browser("r06_approve", phase_review=self.state.review)
        self.state.interrupt("approve")
        return result

    def start_r06(self, **_kwargs) -> BrowserResult:
        self.state.calls.append("start")
        initial, terminal = _executions()
        self.state.execution = terminal
        result = _browser("r06_start", phase_execution=initial)
        self.state.interrupt("start")
        return result


class User:
    def __init__(self, state: State) -> None:
        self.state = state

    def request(self, method: str, path: str, **_kwargs) -> HttpResponse:
        assert method == "GET"
        if path.endswith("/workbench"):
            changes = [] if self.state.review is None else [{
                "id": "change-r06", "context": self.state.context,
            }]
            return HttpResponse(200, {"change_requests": changes}, {})
        if path.endswith("/phase-approval"):
            return HttpResponse(200, {"phase_review": self.state.review}, {})
        if path.endswith("/phase-execution"):
            return HttpResponse(200, {"phase_execution": self.state.execution}, {})
        raise AssertionError(path)


class Effects:
    def __init__(self, state: State) -> None:
        self.state = state
        self.drifted = False
        self.operation_id: str | None = None

    def snapshot(self, scope: RecoveryScope) -> dict[str, object]:
        self.state.calls.append("snapshot")
        return {
            "identity": {
                "candidate_sha256": scope.candidate_sha256,
                "release_inventory_sha256": scope.release_inventory_sha256,
                "kube_context": scope.kube_context,
                "cluster_identity_sha256": scope.cluster_identity_sha256,
            },
            "target": {
                **TARGET, "uid": "verification-uid",
                "resource_version": "11" if self.drifted else "10",
            },
            "annotation_path": ANNOTATION_PATH,
            "annotation_value": (
                f"operator-drift:{self.operation_id}" if self.drifted else None
            ),
            "pod_template_sha256": "f" * 64,
        }

    def drift_metadata(
        self, _scope: RecoveryScope, before: dict[str, object], *, operation_id: str,
    ) -> dict[str, object]:
        self.state.calls.append("drift")
        self.operation_id = operation_id
        self.drifted = True
        result = self._result(before, operation_id)
        self.state.interrupt("drift")
        return result

    def reconcile_metadata_drift(
        self, _scope: RecoveryScope, before: dict[str, object], *, operation_id: str,
    ) -> dict[str, object] | None:
        self.state.calls.append("reconcile-drift")
        return self._result(before, operation_id) if self.drifted else None

    @staticmethod
    def _result(before: dict[str, object], operation_id: str) -> dict[str, object]:
        return {
            "status": "succeeded", "operation_id": operation_id,
            "target": TARGET, "annotation_path": ANNOTATION_PATH,
            "annotation_value": f"operator-drift:{operation_id}",
            "before": before["target"],
            "after": {**before["target"], "resource_version": "11"},  # type: ignore[arg-type]
            "pod_template_sha256": before["pod_template_sha256"],
        }


def _browser(action: str, **summary) -> BrowserResult:
    path = {
        "r06_prepare": "/api/v1/incidents/incident-run-one/change-requests",
        "r06_approve": "/api/v1/change-requests/change-r06/phase-approval/approve",
        "r06_start": "/api/v1/change-requests/change-r06/phase-execution/start",
    }[action]
    request_id = f"request-{action}"
    return BrowserResult(
        {
            "action": action, "same_origin": True,
            "origins": ["https://aiops.example"], "screenshots_masked": True,
            "mutations": [{
                "request_id": request_id, "method": "POST", "path": path,
                "status": 201, "response_request_id": request_id, "identities": {},
            }],
            **summary,
        },
        {f"{action}.png": b"masked"},
    )


def _runner(tmp_path: Path, state: State):
    evidence = recovery_ledger(tmp_path, "R06")
    runner = StaleChangeGateRunner(
        evidence=evidence, effects=Effects(state), console=Console(state),
        user=User(state), base_url="https://aiops.example", sleep=lambda _seconds: None,
    )
    return evidence, runner


def _advance_to_operator(evidence, runner: StaleChangeGateRunner) -> dict[str, str]:
    sre = runner.run_r06(sre_username="sre", sre_password="sre-password")
    attest_recovery_approval(evidence, "R06", sre["review_sha256"])
    return runner.resume_r06(sre_username="sre", sre_password="sre-password")


def test_r06_approves_then_drifts_and_proves_one_stale_zero_mutation(
    tmp_path: Path,
) -> None:
    state = State()
    evidence, runner = _runner(tmp_path, state)

    operator = _advance_to_operator(evidence, runner)
    assert operator["role"] == "platform_operator"
    assert "drift" not in state.calls and "start" not in state.calls
    attest_recovery_drift(evidence, "R06", operator["review_sha256"])
    result = runner.resume_r06(sre_username="sre", sre_password="sre-password")

    assert result == {"gate_id": "R06", "status": "passed", "operations": "4"}
    assert [item for item in state.calls if item in {"prepare", "approve", "drift", "start"}] == [
        "prepare", "approve", "drift", "start",
    ]
    final = evidence.passed_artifact_json("R06", "stale-change.json")["value"]
    assert final["terminal"]["status"] == "stale"
    assert len(final["terminal"]["steps"]) == 1
    assert final["after"]["pod_template_sha256"] == final["operator_drift"]["pod_template_sha256"]


@pytest.mark.parametrize("action", ["prepare", "approve", "drift", "start"])
def test_r06_interruption_reconciles_exact_effect_without_replay(
    tmp_path: Path, action: str,
) -> None:
    state = State(interrupt_action=action)
    evidence, runner = _runner(tmp_path, state)

    if action == "prepare":
        with pytest.raises(KeyboardInterrupt):
            runner.run_r06(sre_username="sre", sre_password="sre-password")
        sre = runner.resume_r06(sre_username="sre", sre_password="sre-password")
    else:
        sre = runner.run_r06(sre_username="sre", sre_password="sre-password")
    attest_recovery_approval(evidence, "R06", sre["review_sha256"])
    if action == "approve":
        with pytest.raises(KeyboardInterrupt):
            runner.resume_r06(sre_username="sre", sre_password="sre-password")
        operator = runner.resume_r06(sre_username="sre", sre_password="sre-password")
    else:
        operator = runner.resume_r06(sre_username="sre", sre_password="sre-password")
    attest_recovery_drift(evidence, "R06", operator["review_sha256"])
    if action in {"drift", "start"}:
        with pytest.raises(KeyboardInterrupt):
            runner.resume_r06(sre_username="sre", sre_password="sre-password")
    result = runner.resume_r06(sre_username="sre", sre_password="sre-password")

    assert result["status"] == "passed"
    assert state.calls.count(action) == 1
    if action == "drift":
        assert state.calls.count("reconcile-drift") == 1


def test_r06_rejects_non_stale_terminal_without_recording_success(tmp_path: Path) -> None:
    state = State()
    evidence, runner = _runner(tmp_path, state)
    operator = _advance_to_operator(evidence, runner)
    attest_recovery_drift(evidence, "R06", operator["review_sha256"])
    initial, terminal = _executions()
    terminal["status"] = "failed"
    terminal["steps"][0]["status"] = "failed"  # type: ignore[index]
    state.execution = terminal

    original = Console.start_r06

    def start_with_failure(self, **kwargs):
        result = original(self, **kwargs)
        self.state.execution = terminal
        return result

    runner.console.start_r06 = start_with_failure.__get__(runner.console, Console)
    with pytest.raises(GateFailed, match="became failed"):
        runner.resume_r06(sre_username="sre", sre_password="sre-password")
    assert evidence.failed_gate == "R06"
