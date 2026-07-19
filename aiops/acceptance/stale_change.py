"""R06 Stale Change gate for the Recovery Module."""

from __future__ import annotations

import re
import time
from dataclasses import asdict
from typing import Callable, Protocol
from urllib.parse import urlsplit

from .evidence import AcceptanceEvidence
from .evidence_types import Artifact
from .integration_support import fail_gate
from .recovery import RecoveryScope, load_recovery_scope, parse_recovery_scope
from .recovery_journal import RecoveryJournal
from .verification_trigger import UserSession


TARGET = {
    "api_version": "apps/v1",
    "kind": "Deployment",
    "namespace": "aiops-verification",
    "name": "verification-api",
}
ANNOTATION_PATH = "/metadata/annotations/aiops.dev~1r06-stale-probe"


class StaleChangeEffects(Protocol):
    def snapshot(self, scope: RecoveryScope) -> dict[str, object]: ...

    def drift_metadata(
        self, scope: RecoveryScope, before: dict[str, object], *, operation_id: str,
    ) -> dict[str, object]: ...

    def reconcile_metadata_drift(
        self, scope: RecoveryScope, before: dict[str, object], *, operation_id: str,
    ) -> dict[str, object] | None: ...


class StaleChangeGateRunner:
    """Owns exact Change approval, fixed out-of-band drift, and stale verification."""

    def __init__(
        self,
        *,
        evidence: AcceptanceEvidence,
        effects: StaleChangeEffects,
        console: object,
        user: UserSession,
        base_url: str,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.evidence = evidence
        self.effects = effects
        self.console = console
        self.user = user
        self.base_url = base_url
        self.sleep = sleep
        self.journal = RecoveryJournal(evidence)

    def run_r06(self, *, sre_username: str, sre_password: str) -> dict[str, str]:
        gate_id = "R06"
        started_at = self.evidence.start_gate(gate_id)
        artifacts: list[Artifact] = []
        secrets = (sre_password,)
        try:
            if not sre_username or not sre_password:
                raise ValueError("R06 requires the SRE Console credential")
            scope = load_recovery_scope(self.evidence)
            before = self.effects.snapshot(scope)
            self._validate_snapshot(before, scope)
            before_artifact = self.evidence.write_json(gate_id, "before.json", before)
            artifacts.append(before_artifact)
            execution = self.evidence.resume_gate(gate_id)
            prepare_id = self.journal.operation_id(gate_id, execution.execution_id, "prepare")
            intent = {
                "scope": asdict(scope),
                "target": TARGET,
                "annotation_path": ANNOTATION_PATH,
                "approved_value": f"user-approved:{prepare_id}",
                "before_sha256": before_artifact.sha256,
            }
            artifacts.append(self.evidence.write_json(gate_id, "intent.json", intent))
            prepared = self._prepare(
                execution, artifacts, scope, intent,
                sre_username=sre_username, sre_password=sre_password,
            )
            review = self._approval_review(artifacts, prepared)
            return {
                "gate_id": gate_id, "status": "awaiting_attestation",
                "role": "sre", "review_sha256": review.sha256,
            }
        except Exception as exc:
            fail_gate(self.evidence, gate_id, artifacts, exc, secrets, started_at)

    def resume_r06(
        self, *, sre_username: str, sre_password: str, attempts: int = 150,
    ) -> dict[str, str]:
        gate_id = "R06"
        execution = self.evidence.resume_gate(gate_id)
        artifacts = list(execution.artifacts)
        secrets = (sre_password,)
        try:
            if not sre_username or not sre_password or attempts < 1:
                raise ValueError("R06 resume requires the SRE credential and polling budget")
            intent = self.journal.artifact_json(artifacts, "intent.json")
            before = self.journal.artifact_json(artifacts, "before.json")
            scope = parse_recovery_scope(intent.get("scope"))
            if (
                scope != load_recovery_scope(self.evidence)
                or intent.get("target") != TARGET
                or intent.get("annotation_path") != ANNOTATION_PATH
                or intent.get("before_sha256")
                != self.journal.artifact(artifacts, "before.json").sha256
            ):
                raise ValueError("R06 durable intent drifted")
            self._validate_snapshot(before, scope)
            prepared = self._prepare(
                execution, artifacts, scope, intent,
                sre_username=sre_username, sre_password=sre_password,
            )
            approval_review = self._approval_review(artifacts, prepared)
            if not self.evidence.attestations_for(gate_id, role="sre"):
                return {
                    "gate_id": gate_id, "status": "awaiting_attestation",
                    "role": "sre", "review_sha256": approval_review.sha256,
                }
            self.journal.require_bound_attestation(
                gate_id, role="sre",
                note=f"approval_review_sha256={approval_review.sha256}",
            )
            approved = self._approve(
                self.evidence.resume_gate(gate_id), artifacts, prepared,
                sre_username=sre_username, sre_password=sre_password,
            )
            drift_review = self._drift_review(artifacts, before, prepared, approved)
            if not self.evidence.attestations_for(gate_id, role="platform_operator"):
                return {
                    "gate_id": gate_id, "status": "awaiting_attestation",
                    "role": "platform_operator", "review_sha256": drift_review.sha256,
                }
            self.journal.require_bound_attestation(
                gate_id, role="platform_operator",
                note=f"drift_review_sha256={drift_review.sha256}",
            )
            drift = self._drift(
                self.evidence.resume_gate(gate_id), artifacts, scope, before,
            )
            started = self._start_execution(
                self.evidence.resume_gate(gate_id), artifacts, prepared, approved,
                sre_username=sre_username, sre_password=sre_password,
            )
            terminal = self._terminal(artifacts, prepared, approved, started, attempts)
            after = self.effects.snapshot(scope)
            self._validate_after(before, after, drift)
            final_projection = self._execution(str(prepared["change_request_id"]))
            self._validate_terminal(final_projection, prepared, approved, started)
            inventory = self._inventory(final_projection)
            if inventory != self._inventory(terminal):
                raise ValueError("R06 terminal Grant/Command inventory changed after stale result")
            final = {
                "scope": asdict(scope), "target": TARGET,
                "prepared": prepared, "approved": approved,
                "operator_drift": drift, "started": started,
                "terminal": terminal, "after": after,
                "grant_command_inventory": inventory,
            }
            artifacts.append(self.evidence.write_json(gate_id, "stale-change.json", final))
            execution = self.evidence.resume_gate(gate_id)
            self.evidence.record_gate(
                gate_id, "passed", artifacts, started_at=execution.started_at,
            )
            return {"gate_id": gate_id, "status": "passed", "operations": "4"}
        except Exception as exc:
            fail_gate(self.evidence, gate_id, artifacts, exc, secrets, execution.started_at)

    def _prepare(
        self, execution, artifacts: list[Artifact], scope: RecoveryScope,
        intent: dict[str, object], *, sre_username: str, sre_password: str,
    ) -> dict[str, object]:
        operation_id = self.journal.operation_id("R06", execution.execution_id, "prepare")
        result = self.journal.effect(
            "R06", execution, artifacts, operation_id=operation_id,
            kind="prepare_stale_change", artifact_name="prepared-change.json",
            dispatch=lambda: self._dispatch_prepare(
                scope, intent, operation_id, sre_username, sre_password,
            ),
            reconcile=lambda: self._reconcile_prepare(scope, intent, operation_id),
        )
        self._validate_prepared(result, intent, self.journal.artifact_json(artifacts, "before.json"))
        return result

    def _dispatch_prepare(
        self, scope: RecoveryScope, intent: dict[str, object], operation_id: str,
        username: str, password: str,
    ) -> dict[str, object]:
        prepare = getattr(self.console, "prepare_r06", None)
        if not callable(prepare):
            raise ValueError("R06 requires the governed Change Console Adapter")
        browser = prepare(
            base_url=self.base_url, username=username, password=password,
            incident_id=scope.incident_id, cluster_id=scope.cluster_id,
            operation_id=operation_id,
            approved_value=str(intent["approved_value"]),
        )
        self._require_browser(browser, "r06_prepare")
        prepared = browser.summary.get("prepared")
        if not isinstance(prepared, dict):
            raise ValueError("R06 Console preparation omitted exact Change facts")
        self._require_mutations(
            browser,
            [f"/api/v1/incidents/{scope.incident_id}/change-requests"],
        )
        return {"status": "succeeded", "operation_id": operation_id, **prepared}

    def _reconcile_prepare(
        self, scope: RecoveryScope, intent: dict[str, object], operation_id: str,
    ) -> dict[str, object] | None:
        response = self.user.request("GET", f"/api/v1/incidents/{scope.incident_id}/workbench")
        changes = response.body.get("change_requests") if response.status == 200 else None
        matches = [
            item for item in changes
            if isinstance(item, dict)
            and item.get("context") == self._context(scope, operation_id, str(intent["approved_value"]))
        ] if isinstance(changes, list) else []
        if len(matches) != 1 or not matches[0].get("id"):
            return None
        review = self._review(str(matches[0]["id"]))
        prepared = self._prepared_from_review(
            scope.incident_id, str(matches[0]["id"]), review,
        )
        return {"status": "succeeded", "operation_id": operation_id, **prepared}

    def _approve(
        self, execution, artifacts: list[Artifact], prepared: dict[str, object],
        *, sre_username: str, sre_password: str,
    ) -> dict[str, object]:
        operation_id = self.journal.operation_id("R06", execution.execution_id, "approve")
        result = self.journal.effect(
            "R06", execution, artifacts, operation_id=operation_id,
            kind="approve_stale_change", artifact_name="approval.json",
            dispatch=lambda: self._dispatch_approve(
                prepared, operation_id, sre_username, sre_password,
            ),
            reconcile=lambda: self._reconcile_approval(prepared, operation_id),
        )
        self._validate_approved(result, prepared)
        return result

    def _dispatch_approve(
        self, prepared: dict[str, object], operation_id: str,
        username: str, password: str,
    ) -> dict[str, object]:
        approve = getattr(self.console, "approve_r06", None)
        if not callable(approve):
            raise ValueError("R06 requires the governed Change Console Approval Adapter")
        browser = approve(
            base_url=self.base_url, username=username, password=password,
            incident_id=str(prepared["incident_id"]), prepared=prepared,
            operation_id=operation_id,
        )
        self._require_browser(browser, "r06_approve")
        self._require_mutations(browser, [
            f"/api/v1/change-requests/{prepared['change_request_id']}/phase-approval/approve",
        ])
        review = browser.summary.get("phase_review")
        if not isinstance(review, dict):
            raise ValueError("R06 Console Approval omitted Phase facts")
        return self._approved_from_review(review, operation_id)

    def _reconcile_approval(
        self, prepared: dict[str, object], operation_id: str,
    ) -> dict[str, object] | None:
        review = self._review(str(prepared["change_request_id"]))
        if review.get("status") != "approved":
            return None
        return self._approved_from_review(review, operation_id)

    def _drift(
        self, execution, artifacts: list[Artifact], scope: RecoveryScope,
        before: dict[str, object],
    ) -> dict[str, object]:
        operation_id = self.journal.operation_id("R06", execution.execution_id, "drift")
        result = self.journal.effect(
            "R06", execution, artifacts, operation_id=operation_id,
            kind="drift_verification_metadata", artifact_name="operator-drift.json",
            dispatch=lambda: self.effects.drift_metadata(
                scope, before, operation_id=operation_id,
            ),
            reconcile=lambda: self.effects.reconcile_metadata_drift(
                scope, before, operation_id=operation_id,
            ),
        )
        if (
            result.get("status") != "succeeded"
            or result.get("operation_id") != operation_id
            or result.get("target") != TARGET
            or result.get("annotation_path") != ANNOTATION_PATH
            or result.get("annotation_value") != f"operator-drift:{operation_id}"
        ):
            raise ValueError("R06 Operator drift is not the fixed metadata operation")
        return result

    def _start_execution(
        self, execution, artifacts: list[Artifact], prepared: dict[str, object],
        approved: dict[str, object], *, sre_username: str, sre_password: str,
    ) -> dict[str, object]:
        operation_id = self.journal.operation_id("R06", execution.execution_id, "start")
        result = self.journal.effect(
            "R06", execution, artifacts, operation_id=operation_id,
            kind="start_stale_change", artifact_name="execution-start.json",
            dispatch=lambda: self._dispatch_start(
                prepared, operation_id, sre_username, sre_password,
            ),
            reconcile=lambda: self._reconcile_start(prepared, operation_id),
        )
        self._validate_started(result, prepared, approved)
        return result

    def _dispatch_start(
        self, prepared: dict[str, object], operation_id: str,
        username: str, password: str,
    ) -> dict[str, object]:
        start = getattr(self.console, "start_r06", None)
        if not callable(start):
            raise ValueError("R06 requires the governed Change Console Execution Adapter")
        browser = start(
            base_url=self.base_url, username=username, password=password,
            incident_id=str(prepared["incident_id"]), prepared=prepared,
            operation_id=operation_id,
        )
        self._require_browser(browser, "r06_start")
        self._require_mutations(browser, [
            f"/api/v1/change-requests/{prepared['change_request_id']}/phase-execution/start",
        ])
        value = browser.summary.get("phase_execution")
        if not isinstance(value, dict):
            raise ValueError("R06 Console start omitted Execution facts")
        return {"status": "succeeded", "operation_id": operation_id, "execution": value}

    def _reconcile_start(
        self, prepared: dict[str, object], operation_id: str,
    ) -> dict[str, object] | None:
        try:
            value = self._execution(str(prepared["change_request_id"]))
        except (RuntimeError, ValueError):
            return None
        return {"status": "succeeded", "operation_id": operation_id, "execution": value}

    def _terminal(
        self, artifacts: list[Artifact], prepared: dict[str, object],
        approved: dict[str, object], started: dict[str, object], attempts: int,
    ) -> dict[str, object]:
        retained = self.journal.optional_artifact_json(artifacts, "stale-result.json")
        if retained is not None:
            self._validate_terminal(retained, prepared, approved, started)
            return retained
        value: dict[str, object] | None = None
        for attempt in range(attempts):
            candidate = self._execution(str(prepared["change_request_id"]))
            status = candidate.get("status")
            if status == "stale":
                value = candidate
                break
            if status not in {"queued", "dispatched", "started"}:
                raise ValueError(f"R06 Execution became {status or 'unknown'}")
            if attempt + 1 < attempts:
                self.sleep(2)
        if value is None:
            raise TimeoutError("R06 Connector did not return stale within polling budget")
        self._validate_terminal(value, prepared, approved, started)
        artifacts.append(self.evidence.write_json("R06", "stale-result.json", value))
        return value

    def _review(self, change_request_id: str) -> dict[str, object]:
        response = self.user.request(
            "GET", f"/api/v1/change-requests/{change_request_id}/phase-approval",
        )
        value = response.body.get("phase_review") if response.status == 200 else None
        if not isinstance(value, dict):
            raise RuntimeError("R06 public Phase review is unavailable")
        return value

    def _execution(self, change_request_id: str) -> dict[str, object]:
        response = self.user.request(
            "GET", f"/api/v1/change-requests/{change_request_id}/phase-execution",
        )
        value = response.body.get("phase_execution") if response.status == 200 else None
        if not isinstance(value, dict):
            raise RuntimeError("R06 public Phase execution is unavailable")
        return value

    @staticmethod
    def _prepared_from_review(
        incident_id: str, change_request_id: str, review: dict[str, object],
    ) -> dict[str, object]:
        changes = review.get("changes")
        change = changes[0] if isinstance(changes, list) and len(changes) == 1 else None
        canonical = change.get("canonical_change") if isinstance(change, dict) else None
        target = canonical.get("target") if isinstance(canonical, dict) else None
        if not isinstance(change, dict) or not isinstance(target, dict):
            raise ValueError("R06 public Phase review omitted exact Change")
        return {
            "incident_id": incident_id,
            "change_request_id": change_request_id,
            "phase_id": review.get("phase_id"),
            "revision_id": review.get("revision_id"),
            "dry_run_hash": change.get("dry_run_hash"),
            "target_confirmation": change.get("target_confirmation"),
            "approval_status": review.get("status"),
            "target_identity": {
                "uid": target.get("uid"), "resource_version": target.get("resource_version"),
            },
            "change_summary": {
                "target": {key: target.get(key) for key in TARGET},
                "operation": canonical.get("operation"),
                "diff": change.get("diff"),
                "post_checks": change.get("post_checks"),
                "rollback": change.get("rollback"),
            },
        }

    @staticmethod
    def _approved_from_review(
        review: dict[str, object], operation_id: str,
    ) -> dict[str, object]:
        return {
            "status": "succeeded", "operation_id": operation_id,
            "phase_id": review.get("phase_id"),
            "revision_id": review.get("revision_id"),
            "approval_status": review.get("status"),
            "approval": review.get("approval"),
        }

    def _approval_review(
        self, artifacts: list[Artifact], prepared: dict[str, object],
    ) -> Artifact:
        value = {
            "gate_id": "R06", "decision": "approve exact stale precondition probe",
            "change_request_id": prepared["change_request_id"],
            "phase_id": prepared["phase_id"], "revision_id": prepared["revision_id"],
            "dry_run_hash": prepared["dry_run_hash"],
            "target_confirmation": prepared["target_confirmation"],
            "target_identity": prepared["target_identity"],
            "change_summary": prepared["change_summary"],
        }
        return self._retained_review(artifacts, "approval-review.json", value)

    def _drift_review(
        self, artifacts: list[Artifact], before: dict[str, object],
        prepared: dict[str, object], approved: dict[str, object],
    ) -> Artifact:
        value = {
            "gate_id": "R06", "decision": "apply fixed out-of-band metadata drift",
            "target": TARGET, "annotation_path": ANNOTATION_PATH,
            "frozen_target": before["target"],
            "change_request_id": prepared["change_request_id"],
            "phase_id": prepared["phase_id"],
            "revision_id": prepared["revision_id"],
            "approval_id": approved["approval"]["id"],  # type: ignore[index]
        }
        return self._retained_review(artifacts, "drift-review.json", value)

    def _retained_review(
        self, artifacts: list[Artifact], name: str, value: dict[str, object],
    ) -> Artifact:
        retained = self.journal.optional_artifact_json(artifacts, name)
        if retained is not None:
            if retained != value:
                raise ValueError(f"R06 retained {name} drifted")
            return self.journal.artifact(artifacts, name)
        artifact = self.evidence.write_json("R06", name, value)
        artifacts.append(artifact)
        return artifact

    @staticmethod
    def _validate_snapshot(value: dict[str, object], scope: RecoveryScope) -> None:
        target = value.get("target")
        if (
            value.get("identity") != {
                "candidate_sha256": scope.candidate_sha256,
                "release_inventory_sha256": scope.release_inventory_sha256,
                "kube_context": scope.kube_context,
                "cluster_identity_sha256": scope.cluster_identity_sha256,
            }
            or not isinstance(target, dict)
            or {key: target.get(key) for key in TARGET} != TARGET
            or not target.get("uid")
            or not target.get("resource_version")
            or re.fullmatch(r"[0-9a-f]{64}", str(value.get("pod_template_sha256"))) is None
        ):
            raise ValueError("R06 exact verification object snapshot is invalid")

    @staticmethod
    def _validate_prepared(
        value: dict[str, object], intent: dict[str, object], before: dict[str, object],
    ) -> None:
        summary = value.get("change_summary")
        diff = summary.get("diff") if isinstance(summary, dict) else None
        checks = summary.get("post_checks") if isinstance(summary, dict) else None
        target = before.get("target")
        if (
            value.get("status") != "succeeded"
            or value.get("approval_status") != "awaiting_approval"
            or not all(value.get(key) for key in (
                "incident_id", "change_request_id", "phase_id", "revision_id",
                "target_confirmation",
            ))
            or re.fullmatch(r"[0-9a-f]{64}", str(value.get("dry_run_hash"))) is None
            or value.get("target_identity") != {
                "uid": target.get("uid") if isinstance(target, dict) else None,
                "resource_version": target.get("resource_version") if isinstance(target, dict) else None,
            }
            or not isinstance(summary, dict)
            or summary.get("target") != TARGET
            or summary.get("operation") != "patch"
            or not isinstance(diff, list) or len(diff) != 1
            or not isinstance(diff[0], dict)
            or diff[0].get("op") not in {"add", "replace"}
            or diff[0].get("path") != ANNOTATION_PATH
            or diff[0].get("after") != intent.get("approved_value")
            or not isinstance(checks, list) or not checks
            or not any(
                isinstance(item, dict) and item.get("type") == "json_pointer"
                and item.get("path") == ANNOTATION_PATH
                for item in checks
            )
        ):
            raise ValueError("R06 prepared Change is not the exact metadata probe")

    @staticmethod
    def _validate_approved(
        value: dict[str, object], prepared: dict[str, object],
    ) -> None:
        approval = value.get("approval")
        frozen = approval.get("frozen_changes") if isinstance(approval, dict) else None
        if (
            value.get("status") != "succeeded"
            or value.get("approval_status") != "approved"
            or value.get("phase_id") != prepared.get("phase_id")
            or value.get("revision_id") != prepared.get("revision_id")
            or not isinstance(approval, dict)
            or not approval.get("id")
            or approval.get("rollback_policy") != "stop_only"
            or not approval.get("authority_ids")
            or not isinstance(frozen, list) or len(frozen) != 1
            or not isinstance(frozen[0], dict)
            or frozen[0].get("dry_run_hash") != prepared.get("dry_run_hash")
            or frozen[0].get("target_confirmation") != prepared.get("target_confirmation")
        ):
            raise ValueError("R06 Approval did not freeze the exact attested Change")

    @staticmethod
    def _validate_started(
        value: dict[str, object], prepared: dict[str, object], approved: dict[str, object],
    ) -> None:
        execution = value.get("execution")
        grant = execution.get("grant") if isinstance(execution, dict) else None
        steps = execution.get("steps") if isinstance(execution, dict) else None
        approval = approved.get("approval")
        if (
            value.get("status") != "succeeded"
            or not isinstance(execution, dict)
            or execution.get("change_request_id") != prepared.get("change_request_id")
            or execution.get("phase_id") != prepared.get("phase_id")
            or execution.get("revision_id") != prepared.get("revision_id")
            or not isinstance(approval, dict)
            or execution.get("approval_id") != approval.get("id")
            or execution.get("rollback_policy") != "stop_only"
            or execution.get("status") not in {"queued", "dispatched", "started", "stale"}
            or not execution.get("id") or not execution.get("command_id")
            or execution.get("grant_count") != 1
            or execution.get("command_count") not in {0, 1}
            or not isinstance(grant, dict) or not grant.get("id")
            or not isinstance(steps, list) or len(steps) != 1
            or not isinstance(steps[0], dict) or steps[0].get("direction") != "forward"
        ):
            raise ValueError("R06 did not start one exact forward execution")

    @staticmethod
    def _validate_terminal(
        value: dict[str, object], prepared: dict[str, object],
        approved: dict[str, object], started: dict[str, object],
    ) -> None:
        initial = started.get("execution")
        steps = value.get("steps")
        step = steps[0] if isinstance(steps, list) and len(steps) == 1 else None
        result = step.get("result") if isinstance(step, dict) else None
        grant = value.get("grant")
        initial_grant = initial.get("grant") if isinstance(initial, dict) else None
        approval = approved.get("approval")
        if (
            value.get("status") != "stale"
            or value.get("change_request_id") != prepared.get("change_request_id")
            or value.get("phase_id") != prepared.get("phase_id")
            or value.get("revision_id") != prepared.get("revision_id")
            or not isinstance(approval, dict) or value.get("approval_id") != approval.get("id")
            or value.get("rollback_policy") != "stop_only"
            or not isinstance(initial, dict)
            or value.get("id") != initial.get("id")
            or value.get("command_id") != initial.get("command_id")
            or value.get("grant_count") != 1
            or value.get("command_count") != 1
            or not value.get("completed_at")
            or value.get("reconciliation") is not None
            or not isinstance(grant, dict) or not isinstance(initial_grant, dict)
            or grant.get("id") != initial_grant.get("id")
            or grant.get("consumed_at") is None or grant.get("revoked_at") is not None
            or not isinstance(step, dict) or step.get("direction") != "forward"
            or step.get("status") != "stale"
            or step.get("command_id") != value.get("command_id")
            or not isinstance(result, dict)
            or result.get("status") != "rejected"
            or result.get("error_code") != "stale_change"
            or any(
                isinstance(item, dict) and item.get("direction") == "rollback"
                for item in steps if isinstance(steps, list)
            )
        ):
            raise ValueError("R06 terminal result is not one zero-mutation Stale Change")

    @staticmethod
    def _validate_after(
        before: dict[str, object], after: dict[str, object], drift: dict[str, object],
    ) -> None:
        old = before.get("target")
        new = after.get("target")
        drift_after = drift.get("after")
        if (
            not isinstance(old, dict) or not isinstance(new, dict)
            or not isinstance(drift_after, dict)
            or {key: new.get(key) for key in TARGET} != TARGET
            or new.get("uid") != old.get("uid")
            or new.get("resource_version") != drift_after.get("resource_version")
            or new.get("resource_version") == old.get("resource_version")
            or after.get("annotation_value") != drift.get("annotation_value")
            or after.get("pod_template_sha256") != before.get("pod_template_sha256")
        ):
            raise ValueError("R06 observed an AIOps mutation or verification target drift")

    @staticmethod
    def _inventory(value: dict[str, object]) -> dict[str, object]:
        steps = value.get("steps")
        return {
            "execution_id": value.get("id"), "command_id": value.get("command_id"),
            "grant_count": value.get("grant_count"),
            "command_count": value.get("command_count"),
            "grant_id": value.get("grant", {}).get("id")
            if isinstance(value.get("grant"), dict) else None,
            "steps": [
                (item.get("direction"), item.get("command_id"), item.get("status"))
                for item in steps if isinstance(item, dict)
            ] if isinstance(steps, list) else None,
        }

    def _require_browser(self, result, action: str) -> None:
        parsed = urlsplit(self.base_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if (
            result.summary.get("action") != action
            or result.summary.get("same_origin") is not True
            or any(item != origin for item in result.summary.get("origins", []))
            or result.summary.get("screenshots_masked") is not True
            or set(result.screenshots) != {f"{action}.png"}
        ):
            raise ValueError(f"R06 {action} browser evidence is invalid")

    @staticmethod
    def _require_mutations(
        result, expected_paths: list[str],
    ) -> None:
        mutations = result.summary.get("mutations")
        paths = [
            item.get("path") for item in mutations if isinstance(item, dict)
        ] if isinstance(mutations, list) else []
        if paths != expected_paths or any(
            not isinstance(item, dict)
            or item.get("method") != "POST"
            or not isinstance(item.get("status"), int)
            or not 200 <= item["status"] < 300
            or item.get("response_request_id") != item.get("request_id")
            for item in mutations if isinstance(mutations, list)
        ):
            raise ValueError("R06 Console mutation sequence is not exact and durable")

    @staticmethod
    def _context(scope: RecoveryScope, operation_id: str, approved_value: str) -> str:
        return (
            "Prepare only; do not execute. Patch only top-level annotation "
            f"aiops.dev/r06-stale-probe={approved_value}. "
            f"cluster_id={scope.cluster_id} operation_id={operation_id}"
        )
