"""First Run Alert-to-Report acceptance gates."""

from __future__ import annotations

import math

from .evidence import Artifact, GateExecution
from .integration_support import fail_gate
from .run_one_decisions import (
    is_verification_incident,
    select_v03_action,
    signal_fingerprints,
    valid_run_id,
    v02_ready,
    v02_public_fact,
)
from .verification_trigger import V01Inputs, VerificationTriggerGateRunner


class RunOneGateRunner(VerificationTriggerGateRunner):
    def run_v02(
        self,
        run_id: str,
        *,
        trigger_started_at: float,
        attempts: int = 90,
    ) -> dict[str, str]:
        started_at = self.evidence.start_gate("V02")
        artifacts: list[Artifact] = []
        try:
            if (
                not valid_run_id(run_id)
                or self.user is None
                or self.telemetry is None
                or not math.isfinite(trigger_started_at)
                or trigger_started_at < 0
                or attempts < 1
            ):
                raise ValueError("V02 requires run identity, User session and telemetry probe")
            intent = {
                "run_id": run_id,
                "trigger_started_at": trigger_started_at,
                "telemetry_deadline_at": trigger_started_at + 120,
                "public_deadline_at": trigger_started_at + 180,
            }
            artifacts.append(
                self.evidence.write_json("V02", "deadline-intent.json", intent)
            )
            return self._complete_v02(
                run_id=run_id,
                intent=intent,
                artifacts=artifacts,
                started_at=started_at,
                attempts=attempts,
                resume=False,
            )
        except Exception as exc:
            fail_gate(self.evidence, "V02", artifacts, exc, (), started_at)

    def resume_v02(self, *, attempts: int = 90) -> dict[str, str]:
        execution = self.evidence.resume_gate("V02")
        artifacts = list(execution.artifacts)
        try:
            intent = self._artifact_json(artifacts, "deadline-intent.json")
            run_id = str(intent.get("run_id") or "")
            if not run_id or attempts < 1:
                raise ValueError("V02 durable deadline intent is invalid")
            return self._complete_v02(
                run_id=run_id,
                intent=intent,
                artifacts=artifacts,
                started_at=execution.started_at,
                attempts=attempts,
                resume=True,
                execution=execution,
            )
        except Exception as exc:
            self._reconcile_unresolved(
                "V02",
                execution,
                outcome="unprovable",
                public_fact={"terminal": False, "reason": "original_deadline_unprovable"},
            )
            fail_gate(
                self.evidence, "V02", artifacts, exc, (), execution.started_at
            )

    def _complete_v02(
        self,
        *,
        run_id: str,
        intent: dict[str, object],
        artifacts: list[Artifact],
        started_at: str,
        attempts: int,
        resume: bool,
        execution: GateExecution | None = None,
    ) -> dict[str, str]:
        if self.user is None or (not resume and self.telemetry is None):
            raise ValueError("V02 completion requires its public fact Adapters")
        if not valid_run_id(run_id):
            raise ValueError("V02 durable run identity is invalid")
        trigger_started_at = float(intent["trigger_started_at"])
        telemetry_deadline_at = float(intent["telemetry_deadline_at"])
        public_deadline_at = float(intent["public_deadline_at"])
        if (
            not all(math.isfinite(item) for item in (
                trigger_started_at, telemetry_deadline_at, public_deadline_at,
            ))
            or trigger_started_at < 0
            or telemetry_deadline_at != trigger_started_at + 120
            or public_deadline_at != trigger_started_at + 180
        ):
            raise ValueError("V02 durable deadline intent is invalid")
        monotonic_started_at = self.monotonic()
        wall_now = self.now()
        if not math.isfinite(monotonic_started_at) or not math.isfinite(wall_now):
            raise ValueError("V02 runner clocks are invalid")
        telemetry_deadline = monotonic_started_at + max(
            0.0, telemetry_deadline_at - wall_now
        )
        public_deadline = monotonic_started_at + max(
            0.0, public_deadline_at - wall_now
        )
        if resume:
            observed = self._artifact_json(artifacts, "signal-paths.json")
        else:
            observed: dict[str, object] | None = None
            for attempt in range(attempts):
                if self.monotonic() > telemetry_deadline:
                    break
                assert self.telemetry is not None
                candidate = self.telemetry.probe_v02(run_id)
                if (
                    self.monotonic() <= telemetry_deadline
                    and v02_ready(
                        candidate,
                        run_id=run_id,
                        trigger_started_at=trigger_started_at,
                        telemetry_deadline_at=telemetry_deadline_at,
                    )
                ):
                    observed = candidate
                    artifacts.append(
                        self.evidence.write_json("V02", "signal-paths.json", observed)
                    )
                    break
                if attempt + 1 < attempts and self.monotonic() < telemetry_deadline:
                    self.sleep(min(2.0, telemetry_deadline - self.monotonic()))
            if observed is None:
                raise TimeoutError("V02 telemetry did not converge within 120s")
        if not v02_ready(
            observed,
            run_id=run_id,
            trigger_started_at=trigger_started_at,
            telemetry_deadline_at=telemetry_deadline_at,
        ):
            raise ValueError("V02 persisted telemetry does not prove the original deadline")
        fingerprints = signal_fingerprints(observed)
        public: dict[str, object] | None = None
        for attempt in range(attempts):
            public = self._v02_public(
                fingerprints=fingerprints,
                trigger_started_at=trigger_started_at,
                public_deadline_at=public_deadline_at,
            )
            if public is not None and (resume or self.monotonic() <= public_deadline):
                break
            public = None
            if attempt + 1 >= attempts or self.monotonic() >= public_deadline:
                break
            self.sleep(min(2.0, public_deadline - self.monotonic()))
        if public is None:
            raise TimeoutError("V02 public Alert/Incident did not converge within 180s")
        artifacts.append(
            self.evidence.write_json("V02", "public-incident.json", public)
        )
        signal = public["alert_signal"]
        incident = public["incident"]
        investigation = public["investigation"]
        assert isinstance(signal, dict) and isinstance(incident, dict)
        assert isinstance(investigation, dict)
        if execution is None:
            execution = self.evidence.resume_gate("V02")
        if any(
            operation.get("kind") != "gate_execution"
            for operation in execution.operations
        ):
            raise ValueError("V02 operation journal contains an unexpected effect")
        self._reconcile_unresolved(
            "V02",
            execution,
            outcome="succeeded",
            public_fact={
                "run_id": run_id,
                "fingerprint": signal["fingerprint"],
                "webhook_request_id": signal["firing_webhook_request_id"],
                "incident_id": incident["id"],
                "investigation_id": investigation["id"],
                "terminal": True,
            },
        )
        self.evidence.record_gate("V02", "passed", artifacts, started_at=started_at)
        return {
            "run_id": run_id,
            "incident_id": str(incident["id"]),
            "investigation_id": str(investigation["id"]),
            "alert_fingerprint": str(signal["fingerprint"]),
            "webhook_request_id": str(signal["firing_webhook_request_id"]),
        }

    def _v02_public(
        self,
        *,
        fingerprints: set[str],
        trigger_started_at: float,
        public_deadline_at: float,
    ) -> dict[str, object] | None:
        assert self.user is not None
        incidents = self.user.request("GET", "/api/v1/incidents")
        if incidents.status != 200:
            raise RuntimeError(f"Incident list returned HTTP {incidents.status}")
        values = incidents.body.get("incidents")
        if not isinstance(values, list):
            raise ValueError("Incident list omitted its public projection")
        matches: list[dict[str, object]] = []
        for incident in values:
            if not isinstance(incident, dict) or not is_verification_incident(incident):
                continue
            incident_id = incident.get("id")
            if not isinstance(incident_id, str) or not incident_id:
                continue
            workbench = self.user.request(
                "GET", f"/api/v1/incidents/{incident_id}/workbench"
            )
            if workbench.status != 200:
                continue
            fact = v02_public_fact(
                workbench.body,
                incident_id=incident_id,
                fingerprints=fingerprints,
                trigger_started_at=trigger_started_at,
                public_deadline_at=public_deadline_at,
            )
            if fact is not None:
                matches.append(fact)
        if len(matches) > 1:
            raise ValueError("V02 correlation resolved more than one public Incident")
        return matches[0] if matches else None

    def run_v03(
        self,
        *,
        run_id: str,
        incident_id: str,
        investigation_id: str,
        alert_fingerprint: str,
        attempts: int = 300,
    ) -> dict[str, str]:
        started_at = self.evidence.start_gate("V03")
        artifacts: list[Artifact] = []
        try:
            if self.user is None or not all(
                (run_id, incident_id, investigation_id, alert_fingerprint)
            ):
                raise ValueError("V03 requires the exact public Incident and Investigation")
            accepted: dict[str, object] | None = None
            model: dict[str, object] | None = None
            action: dict[str, object] | None = None
            for attempt in range(attempts):
                platform = self.user.request("GET", "/api/v1/platform/status")
                workbench = self.user.request(
                    "GET", f"/api/v1/incidents/{incident_id}/workbench"
                )
                if platform.status != 200 or workbench.status != 200:
                    raise RuntimeError("V03 public status/workbench read failed")
                model = platform.body.get("capabilities", {}).get("model")
                decision = select_v03_action(
                    workbench.body,
                    incident_id=incident_id,
                    investigation_id=investigation_id,
                    alert_fingerprint=alert_fingerprint,
                    model=model,
                    now=self.now(),
                )
                investigation = workbench.body.get("investigation")
                if decision is not None:
                    action = decision.action
                    accepted = {
                        "run_id": run_id,
                        "incident": workbench.body.get("incident"),
                        "investigation": workbench.body.get("investigation"),
                        "alert_signal": decision.signal,
                        "evidence_steps": workbench.body.get("evidence_steps"),
                        "judgment": workbench.body.get("judgment"),
                        "recommended_action": action,
                        "model": model,
                    }
                    break
                investigation = workbench.body.get("investigation")
                if isinstance(investigation, dict) and investigation.get("status") not in {
                    "queued", "running",
                }:
                    raise ValueError(
                        "Investigation became terminal without a complete Evidence Gate"
                    )
                if attempt + 1 < attempts:
                    self.sleep(2)
            if accepted is None or model is None or action is None:
                raise TimeoutError("V03 Diagnosis did not complete within 10m")
            artifacts.append(
                self.evidence.write_json("V03", "diagnosis.json", accepted)
            )
            self.evidence.record_gate("V03", "passed", artifacts, started_at=started_at)
            return {
                "run_id": run_id,
                "incident_id": incident_id,
                "investigation_id": investigation_id,
                "model_revision": str(model["configuration_revision"]),
                "recommended_action_id": str(action["id"]),
                "recommended_action_hash": str(action["hash"]),
                "recommended_action_summary": str(action["summary"]),
            }
        except Exception as exc:
            fail_gate(self.evidence, "V03", artifacts, exc, (), started_at)

    def run_v04(
        self,
        *,
        run_id: str,
        incident_id: str,
        recommended_action_id: str,
        recommended_action_hash: str,
        recommended_action_summary: str,
        attempts: int = 300,
    ) -> dict[str, str]:
        started_at = self.evidence.start_gate("V04")
        artifacts: list[Artifact] = []
        try:
            if self.user is None or not all((
                run_id, incident_id, recommended_action_id,
                recommended_action_hash, recommended_action_summary,
            )):
                raise ValueError("V04 requires the exact V03 Recommendation")
            created = self.user.request(
                "POST",
                f"/api/v1/incidents/{incident_id}/change-requests",
                body={
                    "desired_outcome": recommended_action_summary,
                    "context": (
                        f"Use Recommendation {recommended_action_id} with hash "
                        f"{recommended_action_hash}. Controlled verification run_id={run_id}. "
                        "Preserve the exact Incident Deployment scope and recover it through one bounded rollout."
                    ),
                    "idempotency_key": f"a02-v04:{run_id}",
                },
            )
            if created.status not in {200, 201}:
                raise RuntimeError(f"Change Request create returned HTTP {created.status}")
            change = created.body.get("change_request")
            if not isinstance(change, dict) or not change.get("id"):
                raise ValueError("Change Request create omitted its identity")
            change_request_id = str(change["id"])
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
            self.evidence.record_gate("V04", "passed", artifacts, started_at=started_at)
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
            fail_gate(self.evidence, "V04", artifacts, exc, (), started_at)

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
        plan = revision.get("plan") if isinstance(revision, dict) else None
        drafts = plan.get("changes") if isinstance(plan, dict) else None
        changes = review.get("changes")
        if (
            detail.get("id") != change_request_id
            or detail.get("status") != "awaiting_approval"
            or review.get("change_request_id") != change_request_id
            or review.get("status") != "awaiting_approval"
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
            or len(str(change["dry_run_hash"])) != 64
            or not change.get("target_confirmation")
        ):
            raise ValueError("V04 API Server dry-run diff is not exact")
        return change

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
        sre_password: str,
        attempts: int = 150,
    ) -> dict[str, str]:
        started_at = self.evidence.start_gate("V05")
        artifacts: list[Artifact] = []
        secrets = (sre_password,)
        try:
            if self.user is None or not all((
                run_id, incident_id, change_request_id, phase_id, revision_id,
                dry_run_hash, target_confirmation, sre_password,
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
            fresh = self.user.request(
                "POST", "/auth/reauth", body={"password": sre_password},
            )
            if fresh.status != 200:
                raise RuntimeError(f"V05 fresh authentication returned HTTP {fresh.status}")
            approved = self.user.request(
                "POST",
                f"/api/v1/change-requests/{change_request_id}/phase-approval/approve",
                body={
                    "revision_id": revision_id,
                    "dry_run_hashes": [dry_run_hash],
                    "target_confirmations": [target_confirmation],
                    "rollback_policy": "stop_only",
                    "reason": f"Approve the attested A02 controlled rollout for run {run_id}",
                    "idempotency_key": f"a02-v05-approval:{run_id}",
                },
            )
            if approved.status not in {200, 201}:
                raise RuntimeError(f"V05 Approval returned HTTP {approved.status}")
            approved_review = approved.body.get("phase_review")
            approval = approved_review.get("approval") if isinstance(approved_review, dict) else None
            if (
                not isinstance(approval, dict)
                or not approval.get("id")
                or approval.get("rollback_policy") != "stop_only"
                or not approval.get("authority_ids")
                or not isinstance(approval.get("frozen_changes"), list)
                or len(approval["frozen_changes"]) != 1
                or approval["frozen_changes"][0].get("dry_run_hash") != dry_run_hash
                or approval["frozen_changes"][0].get("target_confirmation") != target_confirmation
            ):
                raise ValueError("V05 Approval did not freeze the exact attested Phase")
            started = self.user.request(
                "POST",
                f"/api/v1/change-requests/{change_request_id}/phase-execution/start",
                body={
                    "phase_id": phase_id,
                    "reason": f"Execute the approved A02 controlled rollout for run {run_id}",
                    "idempotency_key": f"a02-v05-execution:{run_id}",
                    "execution_timeout_seconds": 300,
                },
            )
            if started.status not in {200, 201}:
                raise RuntimeError(f"V05 execution start returned HTTP {started.status}")
            initial = started.body.get("phase_execution")
            if not isinstance(initial, dict):
                raise ValueError("V05 execution start omitted its projection")
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
            steps = execution.get("steps")
            terminal_grant = execution.get("grant")
            if (
                execution.get("change_request_id") != change_request_id
                or execution.get("phase_id") != phase_id
                or execution.get("approval_id") != approval["id"]
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
                or any(step.get("direction") == "rollback" for step in steps if isinstance(step, dict))
            ):
                raise ValueError("V05 did not retain one trustworthy forward execution")
            post_checks = steps[0].get("result", {}).get("execution", {}).get("post_checks")
            if (
                not isinstance(post_checks, list)
                or len(post_checks) < 2
                or any(not isinstance(item, dict) or item.get("status") != "succeeded" for item in post_checks)
            ):
                raise ValueError("V05 frozen post-checks did not all succeed")
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
            self.evidence.record_gate("V05", "passed", artifacts, started_at=started_at)
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
