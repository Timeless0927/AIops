"""Governed Change gates for the First Run Module."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from .evidence_types import Artifact, GateExecution, GateResult
from .integration_support import fail_gate
from .verification_trigger import VerificationTriggerGateRunner


class GovernedChangeGateRunner(VerificationTriggerGateRunner):
    def run_v04(
        self,
        *,
        run_id: str,
        incident_id: str,
        recommended_action_id: str,
        recommended_action_hash: str,
        recommended_action_summary: str,
        sre_username: str,
        sre_password: str,
        attempts: int = 300,
    ) -> dict[str, str]:
        started_at = self.evidence.start_gate("V04")
        artifacts: list[Artifact] = []
        try:
            if self.user is None or not all((
                run_id, incident_id, recommended_action_id,
                recommended_action_hash, recommended_action_summary,
                sre_username, sre_password,
            )):
                raise ValueError("V04 requires the exact V03 Recommendation")
            context = (
                f"Use Recommendation {recommended_action_id} with hash "
                f"{recommended_action_hash}. Controlled verification run_id={run_id}. "
                "Preserve the exact Incident Deployment scope and recover it through one bounded rollout."
            )
            artifacts.append(self.evidence.write_json("V04", "intent.json", {
                "run_id": run_id,
                "incident_id": incident_id,
                "recommended_action_id": recommended_action_id,
                "recommended_action_hash": recommended_action_hash,
                "recommended_action_summary": recommended_action_summary,
            }))
            create_v04 = getattr(self.console, "create_v04", None)
            if not callable(create_v04):
                raise ValueError("V04 requires the governed Change Console Adapter")
            browser = create_v04(
                base_url=self.base_url,
                username=sre_username,
                password=sre_password,
                incident_id=incident_id,
                desired_outcome=recommended_action_summary,
                context=context,
            )
            self._verify_browser_origin(browser.summary, "V04")
            change = browser.summary.get("change_request")
            if not isinstance(change, dict) or not change.get("id"):
                raise ValueError("V04 Console create omitted Change Request identity")
            change_request_id = str(change["id"])
            mutations = browser.summary.get("mutations")
            paths = [
                item.get("path") for item in mutations
                if isinstance(item, dict)
            ] if isinstance(mutations, list) else []
            expected_paths = [f"/api/v1/incidents/{incident_id}/change-requests"]
            expired_revision = None
            if change.get("status") == "expired":
                expired_revision = change.get("active_revision")
                expected_paths.append(
                    f"/api/v1/change-requests/{change_request_id}/retry"
                )
            if paths != expected_paths:
                raise ValueError("V04 Console dispatched an unexpected mutation sequence")
            artifacts.append(self.evidence.write_json(
                "V04", "console-create.json", browser.summary,
                known_secrets=(sre_password,),
            ))
            artifacts.extend(
                self.evidence.write_bytes("V04", name, value)
                for name, value in sorted(browser.screenshots.items())
            )
            detail: dict[str, object] | None = None
            review: dict[str, object] | None = None
            for attempt in range(attempts):
                response = self.user.request(
                    "GET", f"/api/v1/change-requests/{change_request_id}",
                )
                if response.status != 200:
                    raise RuntimeError(f"Change Request read returned HTTP {response.status}")
                current = response.body.get("change_request")
                if not isinstance(current, dict):
                    raise ValueError("Change Request read omitted its projection")
                detail = current
                status = str(current.get("status") or "")
                if status == "awaiting_approval":
                    approval = self.user.request(
                        "GET", f"/api/v1/change-requests/{change_request_id}/phase-approval",
                    )
                    if approval.status != 200:
                        raise RuntimeError(f"Phase review returned HTTP {approval.status}")
                    candidate = approval.body.get("phase_review")
                    if not isinstance(candidate, dict):
                        raise ValueError("Phase review omitted its exact diff")
                    review = candidate
                    break
                if status not in {"planning", "validating"}:
                    raise ValueError(f"Change Request became {status or 'unknown'} before dry-run")
                if attempt + 1 < attempts:
                    self.sleep(2)
            if detail is None or review is None:
                raise TimeoutError("V04 Change Plan did not reach awaiting_approval within 10m")
            accepted = self._v04_review(
                detail, review, run_id=run_id, change_request_id=change_request_id,
            )
            if expired_revision is not None:
                current_revision = detail.get("active_revision")
                if (
                    not isinstance(expired_revision, dict)
                    or not isinstance(current_revision, dict)
                    or not expired_revision.get("id")
                    or current_revision.get("id") == expired_revision.get("id")
                    or int(current_revision.get("revision_number", 0))
                    <= int(expired_revision.get("revision_number", 0))
                ):
                    raise ValueError("V04 retry did not retain and replace the expired revision")
            artifacts.append(self.evidence.write_json("V04", "change-plan.json", {
                "run_id": run_id,
                "recommended_action": {
                    "id": recommended_action_id,
                    "hash": recommended_action_hash,
                    "summary": recommended_action_summary,
                },
                "change_request": detail,
                "phase_review": review,
            }))
            self.evidence.record_gate("V04", GateResult("passed", tuple(artifacts)), started_at=started_at)
            return {
                "run_id": run_id,
                "incident_id": incident_id,
                "change_request_id": change_request_id,
                "phase_id": str(review["phase_id"]),
                "revision_id": str(review["revision_id"]),
                "dry_run_hash": str(accepted["dry_run_hash"]),
                "target_confirmation": str(accepted["target_confirmation"]),
            }
        except Exception as exc:
            fail_gate(
                self.evidence, "V04", artifacts, exc, (sre_password,), started_at
            )

    def resume_v04(self) -> dict[str, str]:
        execution = self.evidence.resume_gate("V04")
        artifacts = list(execution.artifacts)
        try:
            intent = self._artifact_json(artifacts, "intent.json")
            incident_id = str(intent["incident_id"])
            run_id = str(intent["run_id"])
            facts = self._proved_console_facts(execution)
            paths = [str(item["path"]) for item in facts]
            create_path = f"/api/v1/incidents/{incident_id}/change-requests"
            if paths[0] != create_path or paths[1:] not in (
                [], [f"/api/v1/change-requests/{self._fact_identity(facts, 'change_request.id')}/retry"],
            ):
                raise ValueError("V04 interrupted mutation sequence is not exact")
            change_request_id = self._fact_identity(facts, "change_request.id")
            detail_response = self.user.request(
                "GET", f"/api/v1/change-requests/{change_request_id}",
            )
            review_response = self.user.request(
                "GET", f"/api/v1/change-requests/{change_request_id}/phase-approval",
            )
            detail = detail_response.body.get("change_request")
            review = review_response.body.get("phase_review")
            if (
                detail_response.status != 200 or review_response.status != 200
                or not isinstance(detail, dict) or not isinstance(review, dict)
            ):
                raise ValueError("V04 interrupted public projection is unprovable")
            accepted = self._v04_review(
                detail, review, run_id=run_id, change_request_id=change_request_id,
            )
            self._reconcile_unresolved(
                "V04", execution, outcome="succeeded",
                public_fact={"change_request_id": change_request_id, "terminal": True},
            )
            artifacts.append(self.evidence.write_json("V04", "change-plan.json", {
                "run_id": run_id,
                "recommended_action": {
                    "id": intent["recommended_action_id"],
                    "hash": intent["recommended_action_hash"],
                    "summary": intent["recommended_action_summary"],
                },
                "change_request": detail,
                "phase_review": review,
            }))
            self.evidence.record_gate("V04", GateResult("passed", tuple(artifacts)), started_at=execution.started_at)
            return {
                "run_id": run_id, "incident_id": incident_id,
                "change_request_id": change_request_id,
                "phase_id": str(review["phase_id"]),
                "revision_id": str(review["revision_id"]),
                "dry_run_hash": str(accepted["dry_run_hash"]),
                "target_confirmation": str(accepted["target_confirmation"]),
            }
        except Exception as exc:
            self._reconcile_unresolved(
                "V04", execution, outcome="unprovable",
                public_fact={"terminal": False, "reason": "public_change_unprovable"},
            )
            fail_gate(self.evidence, "V04", artifacts, exc, (), execution.started_at)

    @staticmethod
    def _v04_review(
        detail: dict[str, object],
        review: dict[str, object],
        *,
        run_id: str,
        change_request_id: str,
    ) -> dict[str, object]:
        annotation = "/spec/template/metadata/annotations/aiops.dev~1verification-run-id"
        expected_target = {
            "api_version": "apps/v1", "kind": "Deployment",
            "namespace": "aiops-verification", "name": "verification-api",
        }
        revision = detail.get("active_revision")
        phase = detail.get("active_phase")
        plan = revision.get("plan") if isinstance(revision, dict) else None
        drafts = plan.get("changes") if isinstance(plan, dict) else None
        changes = review.get("changes")
        if (
            detail.get("id") != change_request_id
            or detail.get("status") != "awaiting_approval"
            or review.get("change_request_id") != change_request_id
            or review.get("status") != "awaiting_approval"
            or not isinstance(phase, dict)
            or review.get("phase_id") != phase.get("id")
            or not isinstance(revision, dict)
            or review.get("revision_id") != revision.get("id")
            or revision.get("validation", {}).get("status") != "succeeded"
            or not isinstance(drafts, list) or len(drafts) != 1
            or not isinstance(changes, list) or len(changes) != 1
        ):
            raise ValueError("V04 did not produce one awaiting-approval Change Plan")
        draft = drafts[0]
        change = changes[0]
        if not isinstance(draft, dict) or not isinstance(change, dict):
            raise ValueError("V04 Change Plan entries are invalid")
        expected_patch = [{"op": "add", "path": annotation, "value": run_id}]
        expected_checks = [
            {"type": "json_pointer", "path": annotation, "operator": "eq", "value": run_id},
            {"type": "workload_rollout"},
        ]
        rollback = draft.get("rollback")
        canonical = change.get("canonical_change")
        target = canonical.get("target") if isinstance(canonical, dict) else None
        payload = canonical.get("payload") if isinstance(canonical, dict) else None
        if (
            draft.get("target") != expected_target
            or draft.get("operation") != "patch"
            or draft.get("payload") != expected_patch
            or draft.get("post_checks") != expected_checks
            or not isinstance(rollback, dict)
            or rollback.get("status") != "unavailable"
            or not isinstance(rollback.get("concrete_loss"), str)
            or not rollback["concrete_loss"]
            or not isinstance(target, dict)
            or {field: target.get(field) for field in expected_target} != expected_target
            or not target.get("uid")
            or not target.get("resource_version")
            or canonical.get("operation") != "patch"
            or not isinstance(payload, list)
            or not payload
            or payload[-1] != expected_patch[0]
            or canonical.get("post_checks") != expected_checks
            or change.get("rollback") != rollback
            or change.get("inverse_change") is not None
            or change.get("post_checks") != expected_checks
        ):
            raise ValueError("V04 plan, preconditions, rollback or post-checks are not exact")
        if any(
            not isinstance(item, dict)
            or item.get("op") not in {"test", "add"}
            or (
                item.get("op") == "add"
                and item.get("path") not in {
                    "/spec/template/metadata/annotations", annotation,
                }
            )
            for item in payload[:-1]
        ):
            raise ValueError("V04 canonical patch contains an unexpected operation")
        diff = change.get("diff")
        if (
            not isinstance(diff, list) or len(diff) != 1
            or not isinstance(diff[0], dict)
            or diff[0].get("op") != "add"
            or diff[0].get("path") != annotation
            or diff[0].get("after") != run_id
            or not isinstance(change.get("dry_run_hash"), str)
            or re.fullmatch(r"[0-9a-f]{64}", str(change["dry_run_hash"])) is None
            or not change.get("target_confirmation")
        ):
            raise ValueError("V04 API Server dry-run diff is not exact")
        return change

    def run_r05(
        self,
        *,
        run_id: str,
        incident_id: str,
        change_request_id: str,
        phase_id: str,
        revision_id: str,
        dry_run_hash: str,
        target_confirmation: str,
        no_authority_username: str,
        no_authority_password: str,
    ) -> dict[str, str]:
        started_at = self.evidence.start_gate("R05")
        artifacts: list[Artifact] = []
        secrets = (no_authority_password,)
        try:
            if self.user is None or not all((
                run_id, incident_id, change_request_id, phase_id,
                revision_id, dry_run_hash, target_confirmation,
                no_authority_username, no_authority_password,
            )):
                raise ValueError("R05 requires the exact V04 Phase and no-Authority User")
            path = f"/api/v1/change-requests/{change_request_id}/phase-execution"
            before = self.user.request("GET", path)
            before_inventory = [] if before.body.get("phase_execution") is None else ["present"]
            if before.status != 200 or before_inventory:
                raise ValueError("R05 requires an empty phase-scoped Grant inventory")
            artifacts.append(self.evidence.write_json("R05", "intent.json", {
                "run_id": run_id, "incident_id": incident_id,
                "change_request_id": change_request_id,
                "phase_id": phase_id, "revision_id": revision_id,
                "dry_run_hash": dry_run_hash, "target_confirmation": target_confirmation,
            }))
            verify_r05 = getattr(self.console, "verify_r05", None)
            if not callable(verify_r05):
                raise ValueError("R05 requires the governed Change Console Adapter")
            browser = verify_r05(
                base_url=self.base_url,
                username=no_authority_username,
                password=no_authority_password,
                incident_id=incident_id,
                change_request_id=change_request_id,
                phase_id=phase_id,
                revision_id=revision_id,
                dry_run_hash=dry_run_hash,
                target_confirmation=target_confirmation,
                run_id=run_id,
            )
            self._verify_browser_origin(browser.summary, "R05")
            if any(browser.summary.get(field) is not False for field in (
                "phase_review_visible",
                "approval_control_visible",
                "execution_control_visible",
            )):
                raise ValueError("R05 no-Authority Console exposed governed Change controls")
            review_path = f"/api/v1/change-requests/{change_request_id}/phase-approval"
            expected_denials = [
                ("GET", review_path),
                ("POST", f"{review_path}/approve"),
                ("POST", f"/api/v1/change-requests/{change_request_id}/phase-execution/start"),
            ]
            denials = browser.summary.get("denials")
            if not isinstance(denials, list) or len(denials) != len(expected_denials):
                raise ValueError("R05 did not return all exact authorization denials")
            request_ids: list[str] = []
            for denial, (method, denial_path) in zip(denials, expected_denials, strict=True):
                if not isinstance(denial, dict):
                    raise ValueError("R05 authorization denial is invalid")
                request_id = denial.get("request_id")
                if (
                    denial.get("method") != method
                    or denial.get("path") != denial_path
                    or denial.get("status") != 404
                    or denial.get("error_code") != "not_found"
                    or denial.get("response_request_id") != request_id
                    or denial.get("payload_keys") != ["error", "request_id", "service", "status"]
                    or denial.get("error_keys") != ["code", "message"]
                    or not isinstance(request_id, str)
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}", request_id) is None
                ):
                    raise ValueError("R05 authorization denial was not exact and bounded")
                request_ids.append(request_id)
            if len(set(request_ids)) != len(request_ids):
                raise ValueError("R05 authorization denial request identities were reused")
            mutations = browser.summary.get("mutations")
            if not isinstance(mutations, list) or len(mutations) != 2:
                raise ValueError("R05 denied mutations were not durably bound")
            for mutation, denial in zip(mutations, denials[1:], strict=True):
                if not isinstance(mutation, dict) or any(
                    mutation.get(field) != denial.get(field)
                    for field in (
                        "request_id", "method", "path", "status",
                        "response_request_id", "error_code",
                    )
                ) or mutation.get("identities") != {}:
                    raise ValueError("R05 denied mutation identity is not reconciled")
            after = self.user.request("GET", path)
            after_inventory = [] if after.body.get("phase_execution") is None else ["present"]
            if after.status != 200 or after_inventory != before_inventory:
                raise ValueError("R05 unauthorized access changed the phase-scoped Grant inventory")
            artifacts.append(self.evidence.write_json(
                "R05",
                "authorization-denial.json",
                {
                    "run_id": run_id,
                    "incident_id": incident_id,
                    "change_request_id": change_request_id,
                    "phase_id": phase_id,
                    "before_grant_inventory": before_inventory,
                    "console": browser.summary,
                    "after_grant_inventory": after_inventory,
                },
                known_secrets=secrets,
            ))
            artifacts.extend(
                self.evidence.write_bytes("R05", name, value)
                for name, value in sorted(browser.screenshots.items())
            )
            self.evidence.record_gate("R05", GateResult("passed", tuple(artifacts)), started_at=started_at)
            return {
                "run_id": run_id,
                "incident_id": incident_id,
                "change_request_id": change_request_id,
                "phase_id": phase_id,
            }
        except Exception as exc:
            fail_gate(self.evidence, "R05", artifacts, exc, secrets, started_at)

    def run_v05(
        self,
        *,
        run_id: str,
        incident_id: str,
        change_request_id: str,
        phase_id: str,
        revision_id: str,
        dry_run_hash: str,
        target_confirmation: str,
        sre_username: str,
        sre_password: str,
        attempts: int = 150,
    ) -> dict[str, str]:
        started_at = self.evidence.start_gate("V05")
        artifacts: list[Artifact] = []
        secrets = (sre_password,)
        try:
            if self.user is None or not all((
                run_id, incident_id, change_request_id, phase_id, revision_id,
                dry_run_hash, target_confirmation, sre_username, sre_password,
            )):
                raise ValueError("V05 requires the exact V04 Phase and SRE fresh-auth credential")
            attestations = self.evidence.require_verified_attestation("V05", role="sre")
            reviewed = self.user.request(
                "GET", f"/api/v1/change-requests/{change_request_id}/phase-approval",
            )
            if reviewed.status != 200:
                raise RuntimeError(f"V05 Phase review returned HTTP {reviewed.status}")
            review = reviewed.body.get("phase_review")
            if not isinstance(review, dict):
                raise ValueError("V05 Phase review is missing")
            changes = review.get("changes")
            if (
                review.get("phase_id") != phase_id
                or review.get("revision_id") != revision_id
                or review.get("status") != "awaiting_approval"
                or not isinstance(changes, list)
                or len(changes) != 1
                or not isinstance(changes[0], dict)
                or changes[0].get("dry_run_hash") != dry_run_hash
                or changes[0].get("target_confirmation") != target_confirmation
                or changes[0].get("rollback", {}).get("status") != "unavailable"
                or changes[0].get("inverse_change") is not None
            ):
                raise ValueError("V05 Phase review no longer matches the attested exact diff")
            artifacts.append(self.evidence.write_json("V05", "intent.json", {
                "run_id": run_id,
                "incident_id": incident_id,
                "change_request_id": change_request_id,
                "phase_id": phase_id,
                "revision_id": revision_id,
                "dry_run_hash": dry_run_hash,
                "target_confirmation": target_confirmation,
            }))
            execute_v05 = getattr(self.console, "execute_v05", None)
            if not callable(execute_v05):
                raise ValueError("V05 requires the governed Change Console Adapter")
            browser = execute_v05(
                base_url=self.base_url,
                username=sre_username,
                password=sre_password,
                incident_id=incident_id,
                change_request_id=change_request_id,
                target_confirmation=target_confirmation,
                run_id=run_id,
            )
            self._verify_browser_origin(browser.summary, "V05")
            mutations = browser.summary.get("mutations")
            paths = [
                item.get("path") for item in mutations
                if isinstance(item, dict)
            ] if isinstance(mutations, list) else []
            if paths != [
                f"/api/v1/change-requests/{change_request_id}/phase-approval/approve",
                f"/api/v1/change-requests/{change_request_id}/phase-execution/start",
            ]:
                raise ValueError("V05 Console dispatched an unexpected mutation sequence")
            approved_review = browser.summary.get("phase_review")
            approval = approved_review.get("approval") if isinstance(approved_review, dict) else None
            approval = self._validate_v05_approval(
                approval, dry_run_hash=dry_run_hash,
                target_confirmation=target_confirmation,
            )
            initial = browser.summary.get("phase_execution")
            if not isinstance(initial, dict):
                raise ValueError("V05 Console execution omitted its projection")
            if (
                initial.get("change_request_id") != change_request_id
                or initial.get("phase_id") != phase_id
                or initial.get("revision_id") != revision_id
                or initial.get("approval_id") != approval["id"]
                or initial.get("idempotent") is not False
            ):
                raise ValueError("V05 Console execution identity is not exact")
            grant = initial.get("grant")
            if (
                not isinstance(grant, dict)
                or not grant.get("id")
                or not 0 < float(grant.get("expires_at", 0)) - float(grant.get("issued_at", 0)) <= 60
            ):
                raise ValueError("V05 execution did not issue a bounded single-use Grant")
            execution: dict[str, object] | None = None
            for attempt in range(attempts):
                response = self.user.request(
                    "GET", f"/api/v1/change-requests/{change_request_id}/phase-execution",
                )
                if response.status != 200:
                    raise RuntimeError(f"V05 execution read returned HTTP {response.status}")
                candidate = response.body.get("phase_execution")
                if not isinstance(candidate, dict):
                    raise ValueError("V05 execution read omitted its projection")
                status = str(candidate.get("status") or "")
                if status == "succeeded":
                    execution = candidate
                    break
                if status not in {"queued", "dispatched", "started"}:
                    raise ValueError(f"V05 execution became {status or 'unknown'}")
                if attempt + 1 < attempts:
                    self.sleep(2)
            if execution is None:
                raise TimeoutError("V05 execution did not succeed within 5m")
            steps, grant = self._validate_v05_terminal(
                execution, initial=initial, approval=approval,
                change_request_id=change_request_id, phase_id=phase_id,
                revision_id=revision_id,
            )
            artifacts.append(self.evidence.write_json(
                "V05", "console-approval-and-start.json", browser.summary,
                known_secrets=secrets,
            ))
            artifacts.extend(
                self.evidence.write_bytes("V05", name, value)
                for name, value in sorted(browser.screenshots.items())
            )
            artifacts.append(self.evidence.write_json(
                "V05",
                "approval-and-execution.json",
                {
                    "run_id": run_id,
                    "incident_id": incident_id,
                    "phase_review": review,
                    "approval": approval,
                    "execution": execution,
                    "attestations": [item["statement"] for item in attestations],
                },
                known_secrets=secrets,
            ))
            self.evidence.record_gate("V05", GateResult("passed", tuple(artifacts)), started_at=started_at)
            return {
                "run_id": run_id,
                "incident_id": incident_id,
                "change_request_id": change_request_id,
                "phase_id": phase_id,
                "revision_id": revision_id,
                "approval_id": str(approval["id"]),
                "execution_id": str(execution["id"]),
                "grant_id": str(grant["id"]),
                "command_id": str(steps[0]["command_id"]),
            }
        except Exception as exc:
            fail_gate(self.evidence, "V05", artifacts, exc, secrets, started_at)

    def resume_v05(self) -> dict[str, str]:
        execution_gate = self.evidence.resume_gate("V05")
        artifacts = list(execution_gate.artifacts)
        try:
            intent = self._artifact_json(artifacts, "intent.json")
            change_request_id = str(intent["change_request_id"])
            phase_id = str(intent["phase_id"])
            revision_id = str(intent["revision_id"])
            facts = self._proved_console_facts(execution_gate)
            if [item["path"] for item in facts] != [
                f"/api/v1/change-requests/{change_request_id}/phase-approval/approve",
                f"/api/v1/change-requests/{change_request_id}/phase-execution/start",
            ]:
                raise ValueError("V05 interrupted mutation sequence is not exact")
            review_response = self.user.request(
                "GET", f"/api/v1/change-requests/{change_request_id}/phase-approval",
            )
            execution_response = self.user.request(
                "GET", f"/api/v1/change-requests/{change_request_id}/phase-execution",
            )
            review = review_response.body.get("phase_review")
            terminal = execution_response.body.get("phase_execution")
            approval = review.get("approval") if isinstance(review, dict) else None
            if (
                review_response.status != 200 or execution_response.status != 200
                or not isinstance(review, dict) or not isinstance(approval, dict)
                or review.get("phase_id") != phase_id
                or review.get("revision_id") != revision_id
                or review.get("status") != "succeeded"
                or not isinstance(terminal, dict) or terminal.get("status") != "succeeded"
            ):
                raise ValueError("V05 interrupted terminal projection is unprovable")
            approval = self._validate_v05_approval(
                approval,
                dry_run_hash=str(intent["dry_run_hash"]),
                target_confirmation=str(intent["target_confirmation"]),
            )
            steps, grant = self._validate_v05_terminal(
                terminal, initial=terminal, approval=approval,
                change_request_id=change_request_id, phase_id=phase_id,
                revision_id=revision_id,
            )
            self._reconcile_unresolved(
                "V05", execution_gate, outcome="succeeded",
                public_fact={"execution_id": terminal["id"], "terminal": True},
            )
            attestations = self.evidence.require_verified_attestation("V05", role="sre")
            artifacts.append(self.evidence.write_json(
                "V05", "approval-and-execution.json", {
                    "run_id": intent["run_id"],
                    "incident_id": intent["incident_id"],
                    "phase_review": review,
                    "approval": approval,
                    "execution": terminal,
                    "attestations": [item["statement"] for item in attestations],
                },
            ))
            self.evidence.record_gate(
                "V05",
                GateResult("passed", tuple(artifacts)),
                started_at=execution_gate.started_at,
            )
            return {
                "run_id": str(intent["run_id"]),
                "incident_id": str(intent["incident_id"]),
                "change_request_id": change_request_id,
                "phase_id": phase_id, "revision_id": revision_id,
                "approval_id": str(approval["id"]),
                "execution_id": str(terminal["id"]),
                "grant_id": str(grant["id"]),
                "command_id": str(steps[0]["command_id"]),
            }
        except Exception as exc:
            self._reconcile_unresolved(
                "V05", execution_gate, outcome="unprovable",
                public_fact={"terminal": False, "reason": "execution_unprovable"},
            )
            fail_gate(
                self.evidence, "V05", artifacts, exc, (), execution_gate.started_at
            )

    @staticmethod
    def _proved_console_facts(execution: GateExecution) -> list[dict[str, object]]:
        operations = [
            item for item in execution.operations if item.get("kind") == "console_mutation"
        ]
        reconciled = {
            item["operation_id"]: item
            for item in execution.reconciliations
            if item.get("outcome") == "succeeded"
        }
        if not operations or any(item["operation_id"] not in reconciled for item in operations):
            raise ValueError("interrupted Console mutation lacks terminal public facts")
        facts = [reconciled[item["operation_id"]].get("public_fact") for item in operations]
        if any(not isinstance(item, dict) for item in facts):
            raise ValueError("interrupted Console mutation fact is invalid")
        return facts  # type: ignore[return-value]

    @staticmethod
    def _fact_identity(facts: list[dict[str, object]], key: str) -> str:
        values = {
            str(identities[key])
            for fact in facts
            for identities in [fact.get("identities")]
            if isinstance(identities, dict) and identities.get(key)
        }
        if len(values) != 1:
            raise ValueError(f"Console mutation identity {key} is not unique")
        return values.pop()

    @staticmethod
    def _validate_v05_approval(
        value: object,
        *,
        dry_run_hash: str,
        target_confirmation: str,
    ) -> dict[str, object]:
        frozen = value.get("frozen_changes") if isinstance(value, dict) else None
        if (
            not isinstance(value, dict)
            or not value.get("id")
            or value.get("rollback_policy") != "stop_only"
            or not value.get("authority_ids")
            or not isinstance(frozen, list)
            or len(frozen) != 1
            or not isinstance(frozen[0], dict)
            or frozen[0].get("dry_run_hash") != dry_run_hash
            or frozen[0].get("target_confirmation") != target_confirmation
        ):
            raise ValueError("V05 Approval did not freeze the exact attested Phase")
        return value

    @staticmethod
    def _validate_v05_terminal(
        execution: dict[str, object],
        *,
        initial: dict[str, object],
        approval: dict[str, object],
        change_request_id: str,
        phase_id: str,
        revision_id: str,
    ) -> tuple[list[dict[str, object]], dict[str, object]]:
        steps = execution.get("steps")
        grant = initial.get("grant")
        terminal_grant = execution.get("grant")
        if (
            execution.get("change_request_id") != change_request_id
            or execution.get("phase_id") != phase_id
            or execution.get("revision_id") != revision_id
            or execution.get("approval_id") != approval["id"]
            or execution.get("id") != initial.get("id")
            or execution.get("command_id") != initial.get("command_id")
            or not isinstance(grant, dict)
            or not grant.get("id")
            or not isinstance(terminal_grant, dict)
            or terminal_grant.get("id") != grant["id"]
            or terminal_grant.get("consumed_at") is None
            or terminal_grant.get("revoked_at") is not None
            or not isinstance(steps, list)
            or len(steps) != 1
            or not isinstance(steps[0], dict)
            or steps[0].get("direction") != "forward"
            or steps[0].get("status") != "succeeded"
            or not steps[0].get("command_id")
            or any(
                step.get("direction") == "rollback"
                for step in steps if isinstance(step, dict)
            )
        ):
            raise ValueError("V05 did not retain one trustworthy forward execution")
        terminal_result = steps[0].get("result")
        effect = terminal_result.get("execution") if isinstance(terminal_result, dict) else None
        post_checks = effect.get("post_checks") if isinstance(effect, dict) else None
        target = effect.get("target") if isinstance(effect, dict) else None
        if (
            not isinstance(effect, dict)
            or effect.get("operation") != "patch"
            or not isinstance(target, dict)
            or target.get("namespace") != "aiops-verification"
            or target.get("name") != "verification-api"
            or not target.get("uid")
            or not target.get("resource_version")
            or not isinstance(post_checks, list)
            or [item.get("type") for item in post_checks if isinstance(item, dict)]
            != ["json_pointer", "workload_rollout"]
            or any(
                not isinstance(item, dict) or item.get("status") != "succeeded"
                for item in post_checks
            )
        ):
            raise ValueError("V05 frozen post-checks did not all succeed")
        return steps, grant

    def _verify_browser_origin(self, summary: dict[str, object], gate_id: str) -> None:
        parsed = urlsplit(self.base_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if summary.get("same_origin") is not True or any(
            value != origin for value in summary.get("origins", [])
        ):
            raise ValueError(f"{gate_id} Console workflow left the Gateway origin")
