from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiops.acceptance.evidence_types import GateResult
from aiops.acceptance.gate_contract import GATE_SEQUENCE
from aiops.acceptance.ledger import AcceptanceLedger, GateFailed
from tests.pilot_acceptance_support import create_evidence, open_evidence
from aiops.acceptance.http import HttpResponse
from aiops.acceptance.run_one import RunOneGateRunner
from aiops.acceptance.web_gates import BrowserResult


def _ledger(tmp_path: Path, gate_id: str) -> AcceptanceLedger:
    evidence = create_evidence(
        tmp_path,
        acceptance_id=f"governed-{gate_id.lower()}",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        acceptance_tool_sha256="c" * 64,
        gate_contract_revision="pilot-clean-acceptance-v4",
        kube_context="pilot-context",
        cluster_identity_sha256="b" * 64,
        access_profile="http_nodeport",
        attestation_verifier=lambda _item: None,
    )
    for predecessor in GATE_SEQUENCE[: GATE_SEQUENCE.index(gate_id)]:
        evidence.start_gate(predecessor)
        evidence.record_gate(predecessor, GateResult("not_applicable" if predecessor == "I04" else "passed", ()))
    return evidence


def _draft() -> dict[str, object]:
    annotation = "/spec/template/metadata/annotations/aiops.dev~1verification-run-id"
    return {
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


def _review(*, status: str = "awaiting_approval", approval: bool = False) -> dict[str, object]:
    draft = _draft()
    annotation = "/spec/template/metadata/annotations/aiops.dev~1verification-run-id"
    canonical = {
        "target": {**draft["target"], "uid": "deployment-uid", "resource_version": "42"},
        "operation": "patch",
        "payload": [
            {"op": "test", "path": "/metadata/uid", "value": "deployment-uid"},
            {"op": "test", "path": "/metadata/resourceVersion", "value": "42"},
            {"op": "add", "path": "/spec/template/metadata/annotations", "value": {}},
            *draft["payload"],
        ],
        "post_checks": draft["post_checks"],
    }
    value: dict[str, object] = {
        "change_request_id": "change-run-one",
        "phase_id": "phase-run-one",
        "revision_id": "revision-run-one",
        "status": status,
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
    }
    if approval:
        value["approval"] = {
            "id": "approval-run-one",
            "authority_ids": ["authority-run-one"],
            "rollback_policy": "stop_only",
            "frozen_changes": [{
                "dry_run_hash": "d" * 64,
                "target_confirmation": "apps/v1:Deployment:aiops-verification/verification-api",
            }],
        }
    return value


def _execution(*, terminal: bool) -> dict[str, object]:
    grant = {
        "id": "grant-run-one", "issued_at": 1000.0, "expires_at": 1060.0,
        "consumed_at": 1001.0 if terminal else None, "revoked_at": None,
    }
    return {
        "id": "execution-run-one",
        "change_request_id": "change-run-one",
        "phase_id": "phase-run-one",
        "revision_id": "revision-run-one",
        "approval_id": "approval-run-one",
        "command_id": "command-run-one",
        "status": "succeeded" if terminal else "queued",
        "idempotent": False,
        "grant": grant,
        "steps": [{
            "id": "step-run-one", "direction": "forward",
            "command_id": "command-run-one", "status": "succeeded",
            "result": {"execution": {
                "operation": "patch",
                "target": {
                    "namespace": "aiops-verification", "name": "verification-api",
                    "uid": "deployment-uid", "resource_version": "43",
                },
                "post_checks": [
                    {"type": "json_pointer", "status": "succeeded"},
                    {"type": "workload_rollout", "status": "succeeded"},
                ],
            }},
        }] if terminal else [],
    }


def _r05_denials() -> list[dict[str, object]]:
    root = "/api/v1/change-requests/change-run-one"
    return [
        {
            "method": method,
            "path": path,
            "status": 404,
            "request_id": f"request-r05-{index}",
            "response_request_id": f"request-r05-{index}",
            "error_code": "not_found",
            "payload_keys": ["error", "request_id", "service", "status"],
            "error_keys": ["code", "message"],
        }
        for index, (method, path) in enumerate((
            ("GET", f"{root}/phase-approval"),
            ("POST", f"{root}/phase-approval/approve"),
            ("POST", f"{root}/phase-execution/start"),
        ), 1)
    ]


class FakeConsole:
    def __init__(self) -> None:
        self.actions: list[str] = []

    def create_v04(self, **_kwargs) -> BrowserResult:
        self.actions.append("v04")
        return BrowserResult({
            "same_origin": True, "origins": ["http://pilot.test"],
            "change_request": {
                "id": "change-run-one", "status": "expired",
                "active_revision": {"id": "revision-expired", "revision_number": 1},
            },
            "mutations": [
                {"path": "/api/v1/incidents/incident-run-one/change-requests"},
                {"path": "/api/v1/change-requests/change-run-one/retry"},
            ],
        }, {"v04.png": b"png"})

    def verify_r05(self, **_kwargs) -> BrowserResult:
        self.actions.append("r05")
        denials = _r05_denials()
        return BrowserResult({
            "same_origin": True, "origins": ["http://pilot.test"],
            "phase_review_visible": False,
            "approval_control_visible": False,
            "execution_control_visible": False,
            "denials": denials,
            "mutations": [
                {
                    **{key: denial[key] for key in (
                        "request_id", "method", "path", "status",
                        "response_request_id", "error_code",
                    )},
                    "identities": {},
                }
                for denial in denials[1:]
            ],
        }, {"r05.png": b"png"})

    def execute_v05(self, **_kwargs) -> BrowserResult:
        self.actions.append("v05")
        return BrowserResult({
            "same_origin": True, "origins": ["http://pilot.test"],
            "phase_review": _review(status="approved", approval=True),
            "phase_execution": _execution(terminal=False),
            "mutations": [
                {"path": "/api/v1/change-requests/change-run-one/phase-approval/approve"},
                {"path": "/api/v1/change-requests/change-run-one/phase-execution/start"},
            ],
        }, {"v05.png": b"png"})


class InterruptedConsole(FakeConsole):
    def __init__(self, evidence: AcceptanceLedger, gate_id: str, *, proved: bool = True) -> None:
        super().__init__()
        self.evidence = evidence
        self.gate_id = gate_id
        self.proved = proved

    def _interrupt(self, facts: list[dict[str, object]]) -> None:
        for index, fact in enumerate(facts, 1):
            operation_id = f"request-{self.gate_id.lower()}-{index}"
            self.evidence.bind_operation(
                self.gate_id, kind="console_mutation", operation_id=operation_id,
            )
            if self.proved:
                self.evidence.reconcile_operation(
                    self.gate_id, operation_id=operation_id,
                    outcome="succeeded", public_fact=fact,
                )
        raise KeyboardInterrupt

    def create_v04(self, **_kwargs) -> BrowserResult:
        self._interrupt([{
            "path": "/api/v1/incidents/incident-run-one/change-requests",
            "identities": {"change_request.id": "change-run-one"},
        }])
        raise AssertionError

    def execute_v05(self, **_kwargs) -> BrowserResult:
        self._interrupt([
            {
                "path": "/api/v1/change-requests/change-run-one/phase-approval/approve",
                "identities": {
                    "phase_review.approval.id": "approval-run-one",
                    "phase_review.revision_id": "revision-run-one",
                },
            },
            {
                "path": "/api/v1/change-requests/change-run-one/phase-execution/start",
                "identities": {
                    "phase_execution.id": "execution-run-one",
                    "phase_execution.revision_id": "revision-run-one",
                },
            },
        ])
        raise AssertionError


class FakeUser:
    def __init__(self, *, r05: bool = False) -> None:
        self.r05 = r05
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, path: str, **_kwargs) -> HttpResponse:
        self.calls.append((method, path))
        if path == "/api/v1/change-requests/change-run-one":
            return HttpResponse(200, {"change_request": {
                "id": "change-run-one", "status": "awaiting_approval",
                "active_phase": {"id": "phase-run-one", "status": "awaiting_approval"},
                "active_revision": {
                    "id": "revision-run-one", "revision_number": 2,
                    "plan": {"summary": "controlled rollout", "changes": [_draft()]},
                    "validation": {"status": "succeeded"},
                },
            }}, {})
        if path.endswith("/phase-approval"):
            return HttpResponse(200, {"phase_review": _review()}, {})
        if path.endswith("/phase-execution"):
            return HttpResponse(
                200, {"phase_execution": None if self.r05 else _execution(terminal=True)}, {}
            )
        raise AssertionError((method, path))


class ResumedUser(FakeUser):
    def request(self, method: str, path: str, **kwargs) -> HttpResponse:
        if path.endswith("/phase-approval"):
            self.calls.append((method, path))
            return HttpResponse(
                200, {"phase_review": _review(status="succeeded", approval=True)}, {}
            )
        return super().request(method, path, **kwargs)


class UnusedCommands:
    def run(self, *_args, **_kwargs):
        raise AssertionError("governed change tests must not invoke shell commands")


def _runner(evidence: AcceptanceLedger, user: FakeUser, console: FakeConsole) -> RunOneGateRunner:
    return RunOneGateRunner(
        evidence=evidence, commands=UnusedCommands(), console=console,
        base_url="http://pilot.test", user=user, sleep=lambda _seconds: None,
    )


def test_v04_creates_and_retries_only_through_console(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path, "V04")
    user, console = FakeUser(), FakeConsole()
    result = _runner(evidence, user, console).run_v04(
        run_id="run-controller-uid-1", incident_id="incident-run-one",
        recommended_action_id="action-run-one", recommended_action_hash="c" * 64,
        recommended_action_summary="Restart verification-api through a controlled rollout",
        sre_username="pilot-sre", sre_password="acceptance-sre-password", attempts=1,
    )
    assert result["revision_id"] == "revision-run-one"
    assert console.actions == ["v04"]
    assert all(method == "GET" for method, _path in user.calls)


def test_r05_hides_governance_and_leaves_execution_grants_empty(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path, "R05")
    user, console = FakeUser(r05=True), FakeConsole()
    result = _runner(evidence, user, console).run_r05(
        run_id="run-controller-uid-1", incident_id="incident-run-one",
        change_request_id="change-run-one", phase_id="phase-run-one",
        revision_id="revision-run-one", dry_run_hash="d" * 64,
        target_confirmation="apps/v1:Deployment:aiops-verification/verification-api",
        no_authority_username="ordinary-user",
        no_authority_password="ordinary-password",
    )
    assert result["phase_id"] == "phase-run-one"
    assert console.actions == ["r05"]
    assert len(user.calls) == 2
    manifest = json.loads(evidence.manifest_path.read_text())
    denial = json.loads(next(tmp_path.rglob("authorization-denial.json")).read_text())
    assert denial["before_grant_inventory"] == denial["after_grant_inventory"] == []
    assert "ordinary-password" not in json.dumps(manifest)


def test_v05_approves_and_starts_once_through_console(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path, "V05")
    statement = evidence.attestation_statement(
        actor="Verification SRE", role="sre", gate_ids=["V05"],
        conclusion="passed", note="exact diff confirmed",
    )
    evidence.append_attestation(
        statement, signature="signature", public_key="ssh-ed25519 test",
        fingerprint="SHA256:test",
    )
    user, console = FakeUser(), FakeConsole()
    result = _runner(evidence, user, console).run_v05(
        run_id="run-controller-uid-1", incident_id="incident-run-one",
        change_request_id="change-run-one", phase_id="phase-run-one",
        revision_id="revision-run-one", dry_run_hash="d" * 64,
        target_confirmation="apps/v1:Deployment:aiops-verification/verification-api",
        sre_username="pilot-sre", sre_password="acceptance-sre-password", attempts=1,
    )
    assert result["approval_id"] == "approval-run-one"
    assert result["execution_id"] == "execution-run-one"
    assert console.actions == ["v05"]
    assert all(method == "GET" for method, _path in user.calls)
    persisted = json.dumps(json.loads(evidence.manifest_path.read_text()))
    assert "acceptance-sre-password" not in persisted


def test_v04_interruption_reconciles_without_replaying_console_mutation(
    tmp_path: Path,
) -> None:
    evidence = _ledger(tmp_path, "V04")
    user = FakeUser()
    with pytest.raises(KeyboardInterrupt):
        _runner(evidence, user, InterruptedConsole(evidence, "V04")).run_v04(
            run_id="run-controller-uid-1", incident_id="incident-run-one",
            recommended_action_id="action-run-one", recommended_action_hash="c" * 64,
            recommended_action_summary="Restart verification-api",
            sre_username="pilot-sre", sre_password="password", attempts=1,
        )
    reopened = open_evidence(evidence.root)
    result = _runner(reopened, FakeUser(), FakeConsole()).resume_v04()
    assert result["change_request_id"] == "change-run-one"


def test_v05_interruption_reconciles_terminal_execution_without_replay(
    tmp_path: Path,
) -> None:
    evidence = _ledger(tmp_path, "V05")
    statement = evidence.attestation_statement(
        actor="Verification SRE", role="sre", gate_ids=["V05"],
        conclusion="passed", note="exact diff confirmed",
    )
    evidence.append_attestation(
        statement, signature="signature", public_key="ssh-ed25519 test",
        fingerprint="SHA256:test",
    )
    with pytest.raises(KeyboardInterrupt):
        _runner(
            evidence, FakeUser(), InterruptedConsole(evidence, "V05")
        ).run_v05(
            run_id="run-controller-uid-1", incident_id="incident-run-one",
            change_request_id="change-run-one", phase_id="phase-run-one",
            revision_id="revision-run-one", dry_run_hash="d" * 64,
            target_confirmation="apps/v1:Deployment:aiops-verification/verification-api",
            sre_username="pilot-sre", sre_password="password", attempts=1,
        )
    reopened = open_evidence(evidence.root)
    result = _runner(reopened, ResumedUser(), FakeConsole()).resume_v05()
    assert result["execution_id"] == "execution-run-one"


def test_unproved_v04_interruption_fails_without_replay(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path, "V04")
    with pytest.raises(KeyboardInterrupt):
        _runner(
            evidence, FakeUser(), InterruptedConsole(evidence, "V04", proved=False)
        ).run_v04(
            run_id="run-controller-uid-1", incident_id="incident-run-one",
            recommended_action_id="action-run-one", recommended_action_hash="c" * 64,
            recommended_action_summary="Restart verification-api",
            sre_username="pilot-sre", sre_password="password", attempts=1,
        )
    reopened = open_evidence(evidence.root)
    with pytest.raises(GateFailed, match="V04"):
        _runner(reopened, FakeUser(), FakeConsole()).resume_v04()


def test_v04_rejects_retry_that_reuses_the_expired_revision(tmp_path: Path) -> None:
    class ReusedRevisionConsole(FakeConsole):
        def create_v04(self, **kwargs) -> BrowserResult:
            result = super().create_v04(**kwargs)
            result.summary["change_request"]["active_revision"] = {  # type: ignore[index]
                "id": "revision-run-one", "revision_number": 2,
            }
            return result

    evidence = _ledger(tmp_path, "V04")
    with pytest.raises(GateFailed, match="V04"):
        _runner(evidence, FakeUser(), ReusedRevisionConsole()).run_v04(
            run_id="run-controller-uid-1", incident_id="incident-run-one",
            recommended_action_id="action-run-one", recommended_action_hash="c" * 64,
            recommended_action_summary="Restart verification-api",
            sre_username="pilot-sre", sre_password="password", attempts=1,
        )


def test_r05_fails_if_any_governed_control_is_visible(tmp_path: Path) -> None:
    class LeakyConsole(FakeConsole):
        def verify_r05(self, **kwargs) -> BrowserResult:
            result = super().verify_r05(**kwargs)
            result.summary["approval_control_visible"] = True
            return result

    evidence = _ledger(tmp_path, "R05")
    with pytest.raises(GateFailed, match="R05"):
        _runner(evidence, FakeUser(r05=True), LeakyConsole()).run_r05(
            run_id="run-controller-uid-1", incident_id="incident-run-one",
            change_request_id="change-run-one", phase_id="phase-run-one",
            revision_id="revision-run-one", dry_run_hash="d" * 64,
            target_confirmation="apps/v1:Deployment:aiops-verification/verification-api",
            no_authority_username="ordinary-user", no_authority_password="password",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [("error_code", "forbidden"), ("payload_keys", ["error", "phase_review", "request_id"])],
)
def test_r05_rejects_inexact_or_leaking_denial(
    tmp_path: Path, field: str, value: object,
) -> None:
    class InvalidDenialConsole(FakeConsole):
        def verify_r05(self, **kwargs) -> BrowserResult:
            result = super().verify_r05(**kwargs)
            result.summary["denials"][0][field] = value  # type: ignore[index]
            return result

    evidence = _ledger(tmp_path, "R05")
    with pytest.raises(GateFailed, match="R05"):
        _runner(evidence, FakeUser(r05=True), InvalidDenialConsole()).run_r05(
            run_id="run-controller-uid-1", incident_id="incident-run-one",
            change_request_id="change-run-one", phase_id="phase-run-one",
            revision_id="revision-run-one", dry_run_hash="d" * 64,
            target_confirmation="apps/v1:Deployment:aiops-verification/verification-api",
            no_authority_username="ordinary-user", no_authority_password="password",
        )


def test_v05_rejects_replayed_execution_start(tmp_path: Path) -> None:
    class ReplayedConsole(FakeConsole):
        def execute_v05(self, **kwargs) -> BrowserResult:
            result = super().execute_v05(**kwargs)
            result.summary["phase_execution"]["idempotent"] = True  # type: ignore[index]
            return result

    evidence = _ledger(tmp_path, "V05")
    statement = evidence.attestation_statement(
        actor="Verification SRE", role="sre", gate_ids=["V05"],
        conclusion="passed", note="exact diff confirmed",
    )
    evidence.append_attestation(
        statement, signature="signature", public_key="ssh-ed25519 test",
        fingerprint="SHA256:test",
    )
    with pytest.raises(GateFailed, match="V05"):
        _runner(evidence, FakeUser(), ReplayedConsole()).run_v05(
            run_id="run-controller-uid-1", incident_id="incident-run-one",
            change_request_id="change-run-one", phase_id="phase-run-one",
            revision_id="revision-run-one", dry_run_hash="d" * 64,
            target_confirmation="apps/v1:Deployment:aiops-verification/verification-api",
            sre_username="pilot-sre", sre_password="password", attempts=1,
        )


def test_v05_does_not_reapprove_a_terminal_phase(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path, "V05")
    statement = evidence.attestation_statement(
        actor="Verification SRE", role="sre", gate_ids=["V05"],
        conclusion="passed", note="exact diff confirmed",
    )
    evidence.append_attestation(
        statement, signature="signature", public_key="ssh-ed25519 test",
        fingerprint="SHA256:test",
    )
    console = FakeConsole()
    with pytest.raises(GateFailed, match="V05"):
        _runner(evidence, ResumedUser(), console).run_v05(
            run_id="run-controller-uid-1", incident_id="incident-run-one",
            change_request_id="change-run-one", phase_id="phase-run-one",
            revision_id="revision-run-one", dry_run_hash="d" * 64,
            target_confirmation="apps/v1:Deployment:aiops-verification/verification-api",
            sre_username="pilot-sre", sre_password="password", attempts=1,
        )
    assert console.actions == []


def test_v05_rejects_stale_terminal_execution(tmp_path: Path) -> None:
    class StaleUser(FakeUser):
        def request(self, method: str, path: str, **kwargs) -> HttpResponse:
            if path.endswith("/phase-execution"):
                self.calls.append((method, path))
                value = _execution(terminal=True)
                value["status"] = "stale"
                return HttpResponse(200, {"phase_execution": value}, {})
            return super().request(method, path, **kwargs)

    evidence = _ledger(tmp_path, "V05")
    statement = evidence.attestation_statement(
        actor="Verification SRE", role="sre", gate_ids=["V05"],
        conclusion="passed", note="exact diff confirmed",
    )
    evidence.append_attestation(
        statement, signature="signature", public_key="ssh-ed25519 test",
        fingerprint="SHA256:test",
    )
    with pytest.raises(GateFailed, match="V05"):
        _runner(evidence, StaleUser(), FakeConsole()).run_v05(
            run_id="run-controller-uid-1", incident_id="incident-run-one",
            change_request_id="change-run-one", phase_id="phase-run-one",
            revision_id="revision-run-one", dry_run_hash="d" * 64,
            target_confirmation="apps/v1:Deployment:aiops-verification/verification-api",
            sre_username="pilot-sre", sre_password="password", attempts=1,
        )
