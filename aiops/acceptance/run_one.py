"""A02 first controlled Alert-to-Report acceptance gates."""

from __future__ import annotations

import json
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .command import CommandExecutor
from .evidence import AcceptanceEvidence, Artifact
from .integration_support import fail_gate
from .web_gates import BrowserResult


@dataclass(frozen=True)
class V01Inputs:
    release_root: Path
    admin_username: str
    admin_password: str
    sre_username: str
    sre_password: str


class V01Console(Protocol):
    def provision_v01(
        self,
        *,
        base_url: str,
        admin_username: str,
        admin_password: str,
        sre_username: str,
        sre_password: str,
    ) -> BrowserResult: ...


class RunSignalProbe(Protocol):
    def probe_v02(self, run_id: str) -> dict[str, object]: ...


class UserSession(Protocol):
    def request(self, method: str, path: str, **kwargs: Any) -> Any: ...


class RunOneGateRunner:
    def __init__(
        self,
        *,
        evidence: AcceptanceEvidence,
        commands: CommandExecutor,
        console: V01Console,
        base_url: str,
        user: UserSession | None = None,
        telemetry: RunSignalProbe | None = None,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.evidence = evidence
        self.commands = commands
        self.console = console
        self.base_url = base_url
        self.user = user
        self.telemetry = telemetry
        self.now = now
        self.sleep = sleep

    def run_v01(self, inputs: V01Inputs) -> dict[str, object]:
        started_at = self.evidence.start_gate("V01")
        artifacts: list[Artifact] = []
        secrets = (inputs.admin_password, inputs.sre_password)
        try:
            base = inputs.release_root / "verification/base"
            run = inputs.release_root / "verification/run"
            apply_base = self.commands.run(
                ["kubectl", "apply", "-k", str(base)], timeout=60
            )
            wait_base = self.commands.run(
                [
                    "kubectl", "wait", "-n", "aiops-verification",
                    "--for=condition=Available", "deployment/verification-api",
                    "--timeout=2m",
                ],
                timeout=150,
            )
            browser = self.console.provision_v01(
                base_url=self.base_url,
                admin_username=inputs.admin_username,
                admin_password=inputs.admin_password,
                sre_username=inputs.sre_username,
                sre_password=inputs.sre_password,
            )
            self._verify_console(browser)
            apply_run = self.commands.run(
                ["kubectl", "apply", "-k", str(run)], timeout=60
            )
            wait_run = self.commands.run(
                [
                    "kubectl", "wait", "-n", "aiops-verification",
                    "--for=condition=complete", "job/verification-trigger",
                    "--timeout=2m",
                ],
                timeout=150,
            )
            job_result = self.commands.run(
                [
                    "kubectl", "get", "job/verification-trigger",
                    "-n", "aiops-verification", "-o", "json",
                ],
                timeout=30,
            )
            log_result = self.commands.run(
                [
                    "kubectl", "logs", "job/verification-trigger",
                    "-n", "aiops-verification",
                ],
                timeout=30,
            )
            results = (apply_base, wait_base, apply_run, wait_run, job_result, log_result)
            if any(result.exit_code != 0 for result in results):
                raise RuntimeError("verification fixture apply or trigger failed")
            job = json.loads(job_result.stdout)
            run_id = str(job.get("metadata", {}).get("uid", ""))
            if not run_id or job.get("status", {}).get("succeeded") != 1:
                raise ValueError("verification trigger Job did not expose one successful run ID")
            trigger_events = [
                json.loads(line) for line in log_result.stdout.splitlines() if line.strip()
            ]
            trigger = next(
                (
                    item for item in trigger_events
                    if item == {
                        "event": "verification_trigger_job_succeeded",
                        "run_id": run_id,
                    }
                ),
                None,
            )
            if trigger is None:
                raise ValueError("verification trigger output does not match the Job controller UID")
            artifacts.extend([
                self.evidence.write_text(
                    "V01",
                    "fixture-commands.txt",
                    "\n".join(self.evidence.command_text(result) for result in results),
                    known_secrets=secrets,
                ),
                self.evidence.write_json(
                    "V01", "console-provision.json", browser.summary,
                    known_secrets=secrets,
                ),
                self.evidence.write_json(
                    "V01",
                    "run.json",
                    {"run_id": run_id, "job": job, "trigger": trigger},
                ),
            ])
            artifacts.extend(
                self.evidence.write_bytes("V01", name, value)
                for name, value in sorted(browser.screenshots.items())
            )
            self.evidence.record_gate("V01", "passed", artifacts, started_at=started_at)
            return {"run_id": run_id, **browser.summary}
        except Exception as exc:
            fail_gate(
                self.evidence, "V01", artifacts, exc, secrets, started_at
            )

    def run_v02(self, run_id: str, *, attempts: int = 90) -> dict[str, str]:
        started_at = self.evidence.start_gate("V02")
        artifacts: list[Artifact] = []
        try:
            if not run_id or self.user is None or self.telemetry is None:
                raise ValueError("V02 requires run identity, User session and telemetry probe")
            observed: dict[str, object] | None = None
            public: dict[str, object] | None = None
            for attempt in range(attempts):
                observed = self.telemetry.probe_v02(run_id)
                fingerprints = self._signal_fingerprints(observed)
                incidents = self.user.request("GET", "/api/v1/incidents")
                if incidents.status != 200:
                    raise RuntimeError(f"Incident list returned HTTP {incidents.status}")
                for incident in incidents.body.get("incidents", []):
                    if not self._is_verification_incident(incident):
                        continue
                    workbench = self.user.request(
                        "GET", f"/api/v1/incidents/{incident['id']}/workbench"
                    )
                    if workbench.status != 200:
                        continue
                    signal = next(
                        (
                            item for item in workbench.body.get("alert_signals", [])
                            if item.get("fingerprint") in fingerprints
                            and item.get("alertname") == "AIOpsVerificationWorkloadUnavailable"
                            and item.get("status") == "firing"
                        ),
                        None,
                    )
                    investigation = workbench.body.get("investigation")
                    if signal and isinstance(investigation, dict) and investigation.get("id"):
                        public = {
                            "incident": workbench.body.get("incident"),
                            "investigation": investigation,
                            "alert_signal": signal,
                        }
                        break
                if public is not None and self._v02_ready(observed):
                    break
                if attempt + 1 < attempts:
                    self.sleep(2)
            if observed is None or public is None or not self._v02_ready(observed):
                raise TimeoutError("V02 real signal paths did not converge within 3m")
            artifacts.extend([
                self.evidence.write_json("V02", "signal-paths.json", observed),
                self.evidence.write_json("V02", "public-incident.json", public),
            ])
            self.evidence.record_gate("V02", "passed", artifacts, started_at=started_at)
            signal = public["alert_signal"]
            incident = public["incident"]
            investigation = public["investigation"]
            assert isinstance(signal, dict) and isinstance(incident, dict)
            assert isinstance(investigation, dict)
            return {
                "run_id": run_id,
                "incident_id": str(incident["id"]),
                "investigation_id": str(investigation["id"]),
                "alert_fingerprint": str(signal["fingerprint"]),
            }
        except Exception as exc:
            fail_gate(self.evidence, "V02", artifacts, exc, (), started_at)

    @staticmethod
    def _is_verification_incident(incident: dict[str, object]) -> bool:
        return (
            incident.get("alertname") == "AIOpsVerificationWorkloadUnavailable"
            and incident.get("cluster_id") == "pilot-cluster"
            and incident.get("namespace") == "aiops-verification"
            and incident.get("workload_name") == "verification-api"
        )

    @staticmethod
    def _signal_fingerprints(observed: dict[str, object]) -> set[str]:
        prometheus = {
            str(item.get("fingerprint"))
            for item in observed.get("prometheus_alerts", [])
            if isinstance(item, dict) and item.get("state") == "firing"
        }
        alertmanager = {
            str(item.get("fingerprint"))
            for item in observed.get("alertmanager_alerts", [])
            if isinstance(item, dict) and item.get("status") == "active"
        }
        return prometheus & alertmanager

    @classmethod
    def _v02_ready(cls, observed: dict[str, object]) -> bool:
        return (
            int(observed.get("fault_metric_series", 0)) >= 1
            and int(observed.get("deployment_unavailable_series", 0)) >= 1
            and int(observed.get("activation_log_lines", 0)) >= 1
            and bool(cls._signal_fingerprints(observed))
        )

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
                action = self._v03_action(
                    workbench.body,
                    investigation_id=investigation_id,
                    alert_fingerprint=alert_fingerprint,
                )
                if action is not None and self._model_ready(model):
                    accepted = {
                        "run_id": run_id,
                        "incident": workbench.body.get("incident"),
                        "investigation": workbench.body.get("investigation"),
                        "alert_signal": next(
                            item for item in workbench.body.get("alert_signals", [])
                            if item.get("fingerprint") == alert_fingerprint
                        ),
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
            post_checks = steps[0].get("result", {}).get("post_checks")
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

    @staticmethod
    def _model_ready(value: object) -> bool:
        if not isinstance(value, dict):
            return False
        revision = value.get("configuration_revision")
        return (
            value.get("readiness") == "ready"
            and isinstance(revision, str)
            and bool(revision)
            and value.get("verification", {}).get("state") == "verified"
            and value.get("verification", {}).get("revision") == revision
            and value.get("availability", {}).get("state") == "available"
        )

    def _v03_action(
        self,
        workbench: dict[str, object],
        *,
        investigation_id: str,
        alert_fingerprint: str,
    ) -> dict[str, object] | None:
        investigation = workbench.get("investigation")
        if (
            not isinstance(investigation, dict)
            or investigation.get("id") != investigation_id
            or investigation.get("status") != "completed"
            or not any(
                item.get("fingerprint") == alert_fingerprint
                and item.get("status") == "firing"
                for item in workbench.get("alert_signals", [])
            )
        ):
            return None
        steps = {
            str(item.get("id")): item
            for item in workbench.get("evidence_steps", [])
            if isinstance(item, dict)
            and item.get("state") == "succeeded"
            and float(item.get("expires_at", 0)) > self.now()
            and 0 <= self.now() - float(item.get("observed_at", 0)) <= 120
            and self._exact_verification_scope(item.get("scope"))
        }
        judgment = workbench.get("judgment")
        if not isinstance(judgment, dict) or judgment.get("evidence_gate_status") != "complete":
            return None
        for action in workbench.get("recommended_actions", []):
            if not isinstance(action, dict):
                continue
            selected = [steps.get(str(step_id)) for step_id in action.get("evidence_step_ids", [])]
            sources = {str(item.get("source")) for item in selected if isinstance(item, dict)}
            if (
                action.get("change_intent") == "controlled_restart"
                and action.get("stale") is False
                and action.get("gate", {}).get("status") == "complete"
                and self._exact_verification_scope(action.get("target"))
                and {"prometheus", "loki", "k8s"} <= sources
            ):
                return action
        return None

    @staticmethod
    def _exact_verification_scope(value: object) -> bool:
        return isinstance(value, dict) and (
            value.get("cluster_id") == "pilot-cluster"
            and value.get("namespace") == "aiops-verification"
            and value.get("workload_kind") == "Deployment"
            and value.get("workload_name") == "verification-api"
        )

    def _verify_console(self, browser: BrowserResult) -> None:
        parsed = urllib.parse.urlsplit(self.base_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if browser.summary.get("same_origin") is not True or any(
            item != origin for item in browser.summary.get("origins", [])
        ):
            raise ValueError("V01 Console workflow left the Gateway origin")
        cluster = browser.summary.get("cluster", {})
        binding = browser.summary.get("binding", {})
        authority = browser.summary.get("authority", {})
        scope = authority.get("scope", {}) if isinstance(authority, dict) else {}
        if (
            not isinstance(cluster, dict)
            or cluster.get("cluster_id") != "pilot-cluster"
            or cluster.get("environment") not in {"dev", "test", "staging"}
            or cluster.get("mutation_enabled") is not True
            or not isinstance(binding, dict)
            or not binding.get("deployment_target_id")
            or binding.get("namespace") != "aiops-verification"
            or binding.get("workload_kind") != "Deployment"
            or binding.get("workload_name") != "verification-api"
            or not isinstance(authority, dict)
            or authority.get("environment") != cluster.get("environment")
            or authority.get("scope_type") != "namespace"
            or scope != {
                "cluster_id": "pilot-cluster",
                "namespace": "aiops-verification",
            }
        ):
            raise ValueError("V01 Console provisioning did not bind the exact non-production scope")
        for owner in ("sre", "team", "service", "binding", "authority"):
            item = browser.summary.get(owner)
            if not isinstance(item, dict) or not item.get("id"):
                raise ValueError(f"V01 Console provisioning omitted {owner} identity")
