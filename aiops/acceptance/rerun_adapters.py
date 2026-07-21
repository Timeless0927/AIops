"""Fixed external effects for the V08 second verification run."""

from __future__ import annotations

import math
import re
import time
from typing import Any, Callable
from urllib.parse import urlsplit

from .command import CommandExecutor, CommandResult
from .rerun import RerunScope
from .run_one_decisions import (
    select_v03_action,
    signal_fingerprints,
    valid_run_id,
    v02_public_fact,
    v02_ready,
)
from .verification_run import parse_verification_run


class KubectlRerunAdapter:
    def __init__(self, commands: CommandExecutor, *, kube_context: str) -> None:
        if not kube_context:
            raise ValueError("V08 Kubernetes Adapter requires the frozen context")
        self.commands = commands
        self.kube_context = kube_context

    def trigger(self, scope: RerunScope, *, operation_id: str) -> dict[str, object]:
        self._require_scope(scope, operation_id)
        results = [
            self._run([
                "delete", "job", "verification-trigger", "-n", "aiops-verification",
                "--ignore-not-found", "--wait=true",
            ], timeout=60),
            self._run(["create", "-k", str(scope.release_root / "verification/run")], timeout=60),
            self._run([
                "wait", "-n", "aiops-verification", "--for=condition=complete",
                "job/verification-trigger", "--timeout=2m",
            ], timeout=150),
        ]
        if any(result.exit_code != 0 for result in results):
            raise RuntimeError("V08 verification Job replacement failed")
        return self._terminal(scope, operation_id)

    def reconcile_trigger(
        self, scope: RerunScope, *, operation_id: str,
    ) -> dict[str, object] | None:
        self._require_scope(scope, operation_id)
        job = self._run([
            "get", "job/verification-trigger", "-n", "aiops-verification", "-o", "json",
        ], timeout=30)
        log = self._run([
            "logs", "job/verification-trigger", "-n", "aiops-verification",
        ], timeout=30)
        if job.exit_code != 0 or log.exit_code != 0:
            return None
        return self._parsed(scope, operation_id, job, log)

    def _terminal(self, scope: RerunScope, operation_id: str) -> dict[str, object]:
        job = self._run([
            "get", "job/verification-trigger", "-n", "aiops-verification", "-o", "json",
        ], timeout=30)
        log = self._run([
            "logs", "job/verification-trigger", "-n", "aiops-verification",
        ], timeout=30)
        if job.exit_code != 0 or log.exit_code != 0:
            raise RuntimeError("V08 verification Job terminal facts are unavailable")
        return self._parsed(scope, operation_id, job, log)

    @staticmethod
    def _parsed(
        scope: RerunScope, operation_id: str, job: CommandResult, log: CommandResult,
    ) -> dict[str, object]:
        job_value, trigger, run_id, trigger_started_at = parse_verification_run(
            job.stdout, log.stdout,
        )
        if run_id == scope.old_run_id:
            raise ValueError("V08 reused the first verification run identity")
        return {
            "status": "succeeded", "operation_id": operation_id,
            "run_id": run_id, "trigger_started_at": trigger_started_at,
            "job": job_value, "trigger": trigger,
        }

    def _run(self, args: list[str], *, timeout: int) -> CommandResult:
        return self.commands.run(
            ["kubectl", "--context", self.kube_context, *args], timeout=timeout,
        )

    @staticmethod
    def _require_scope(scope: RerunScope, operation_id: str) -> None:
        if (
            not scope.release_root.is_absolute()
            or not valid_run_id(scope.old_run_id)
            or not scope.incident_id
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/._:-]{0,299}", operation_id) is None
        ):
            raise ValueError("V08 fixed rerun request is invalid")


class GatewayRerunChainAdapter:
    """Drives the existing Console and actor-scoped projections for V08."""

    def __init__(
        self, *, console: Any, user: Any, notification_admin: Any, telemetry: Any,
        base_url: str, sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time, attempts: int = 300,
    ) -> None:
        if not base_url or attempts < 1:
            raise ValueError("V08 Gateway Adapter requires an origin and positive attempts")
        self.console = console
        self.user = user
        self.notification_admin = notification_admin
        self.telemetry = telemetry
        self.base_url = base_url
        self.sleep = sleep
        self.now = now
        self.attempts = attempts

    def prepare(
        self, scope: RerunScope, trigger: dict[str, object], *, operation_id: str,
        sre_username: str, sre_password: str,
        no_authority_username: str, no_authority_password: str,
        platform_admin_username: str, platform_admin_password: str,
    ) -> dict[str, object]:
        public = self._public_signal(scope, trigger)
        old_investigation = public.get("investigation")
        if (
            not isinstance(old_investigation, dict)
            or old_investigation.get("id") != scope.old_investigation_id
        ):
            raise ValueError("V08 signal did not reopen the first Incident history")
        browser = self.console.reinvestigate_v08(
            base_url=self.base_url, username=sre_username, password=sre_password,
            incident_id=scope.incident_id,
        )
        self._browser(browser, "v08_reinvestigate", [
            f"/api/v1/incidents/{scope.incident_id}/reinvestigate",
        ])
        investigation = browser.summary.get("investigation")
        if not isinstance(investigation, dict) or not investigation.get("id"):
            raise ValueError("V08 Console reinvestigation omitted its identity")
        diagnosis = self._diagnosis(
            scope, str(investigation["id"]), str(trigger["run_id"]),
            str(public["alert_signal"]["fingerprint"]),  # type: ignore[index]
        )
        action = diagnosis["recommended_action"]
        assert isinstance(action, dict)
        created = self.console.create_v08(
            base_url=self.base_url, username=sre_username, password=sre_password,
            incident_id=scope.incident_id, run_id=str(trigger["run_id"]),
            desired_outcome=str(action["summary"]),
        )
        self._browser(created, "v08_create", [
            f"/api/v1/incidents/{scope.incident_id}/change-requests",
        ], allow_retry=True)
        change = created.summary.get("change_request")
        if not isinstance(change, dict) or not change.get("id"):
            raise ValueError("V08 Console create omitted Change Request identity")
        detail, review = self._change_review(str(change["id"]))
        [exact] = review["changes"]  # type: ignore[index]
        denial = self.console.verify_v08_denial(
            base_url=self.base_url,
            username=no_authority_username, password=no_authority_password,
            incident_id=scope.incident_id, change_request_id=str(change["id"]),
            phase_id=str(review["phase_id"]), revision_id=str(review["revision_id"]),
            dry_run_hash=str(exact["dry_run_hash"]),
            target_confirmation=str(exact["target_confirmation"]),
            run_id=str(trigger["run_id"]),
        )
        root = f"/api/v1/change-requests/{change['id']}"
        self._browser(denial, "v08_denial", [
            f"{root}/phase-approval/approve", f"{root}/phase-execution/start",
        ])
        self._denial(denial, str(change["id"]))
        report_v1 = self._report_v1(scope)
        destination, receipt_review = self._destination_receipt(
            scope,
            platform_admin_username=platform_admin_username,
            platform_admin_password=platform_admin_password,
        )
        evidence_steps = diagnosis.get("evidence_steps")
        return {
            "status": "succeeded", "operation_id": operation_id,
            "run_id": trigger["run_id"], "incident_id": scope.incident_id,
            "alert_fingerprint": public["alert_signal"]["fingerprint"],  # type: ignore[index]
            "investigation_id": investigation["id"],
            "investigation_sequence": investigation["sequence"],
            "evidence_step_ids": [item["id"] for item in evidence_steps],  # type: ignore[index]
            "recommended_action_id": action["id"],
            "change_request_id": change["id"], "phase_id": review["phase_id"],
            "revision_id": review["revision_id"], "dry_run_hash": exact["dry_run_hash"],
            "target_confirmation": exact["target_confirmation"],
            "authorization_denials": 3, "grant_count": 0, "command_count": 0,
            "approval_review": {
                "change_request": detail, "phase_review": review,
                "recommended_action": action,
            },
            "report_v1": report_v1,
            "destination": destination,
            "receipt_review": receipt_review,
        }

    def reconcile_prepare(
        self, _scope: RerunScope, _trigger: dict[str, object], *, operation_id: str,
    ) -> dict[str, object] | None:
        # The denied Console probes have no durable success projection; interruption is fail-closed.
        return None

    def execute(
        self, scope: RerunScope, prepared: dict[str, object], *, operation_id: str,
        sre_username: str, sre_password: str,
    ) -> dict[str, object]:
        browser = self.console.execute_v08(
            base_url=self.base_url, username=sre_username, password=sre_password,
            incident_id=scope.incident_id,
            change_request_id=str(prepared["change_request_id"]),
            target_confirmation=str(prepared["target_confirmation"]),
            run_id=str(prepared["run_id"]),
        )
        root = f"/api/v1/change-requests/{prepared['change_request_id']}"
        self._browser(browser, "v08_execute", [
            f"{root}/phase-approval/approve", f"{root}/phase-execution/start",
        ])
        terminal = self._terminal_execution(str(prepared["change_request_id"]))
        recovery = self._resolved_recovery(scope, prepared, terminal)
        resolution = self._resolution_identity(scope, prepared, recovery)
        report = self._ready_report(scope, str(prepared["investigation_id"]))
        grant = terminal.get("grant")
        steps = terminal.get("steps")
        approval = browser.summary.get("phase_review", {}).get("approval")
        if (
            terminal.get("grant_count") != 1 or terminal.get("command_count") != 1
            or not isinstance(grant, dict) or not grant.get("id")
            or grant.get("consumed_at") is None or grant.get("revoked_at") is not None
            or not isinstance(steps, list) or len(steps) != 1
            or not isinstance(steps[0], dict) or not steps[0].get("command_id")
            or not self._typed_success(steps[0])
            or not isinstance(approval, dict) or not approval.get("id")
        ):
            raise ValueError("V08 terminal execution identity inventory is incomplete")
        return {
            "status": "succeeded", "operation_id": operation_id,
            **{key: prepared[key] for key in (
                "run_id", "incident_id", "investigation_id", "change_request_id",
                "phase_id", "revision_id",
            )},
            "approval_id": approval["id"], "grant_id": grant["id"],
            "command_id": steps[0]["command_id"], "execution_id": terminal["id"],
            "execution_status": terminal["status"], "recovery_status": recovery["status"],
            "resolution": resolution,
            "report_review": report["draft"], "report_v1": self._report_v1(scope),
            "destination": prepared["destination"],
        }

    def reconcile_execute(
        self, scope: RerunScope, prepared: dict[str, object], *, operation_id: str,
    ) -> dict[str, object] | None:
        try:
            review = self._request(
                f"/api/v1/change-requests/{prepared['change_request_id']}/phase-approval",
            )["phase_review"]
            terminal = self._terminal_execution(str(prepared["change_request_id"]), attempts=1)
            recovery = self._resolved_recovery(scope, prepared, terminal, attempts=1)
            resolution = self._resolution_identity(scope, prepared, recovery)
            report = self._ready_report(scope, str(prepared["investigation_id"]))
            grant, steps = terminal.get("grant"), terminal.get("steps")
            approval = review.get("approval") if isinstance(review, dict) else None
            if not isinstance(grant, dict) or not isinstance(steps, list) or len(steps) != 1:
                return None
            return {
                "status": "succeeded", "operation_id": operation_id,
                **{key: prepared[key] for key in (
                    "run_id", "incident_id", "investigation_id", "change_request_id",
                    "phase_id", "revision_id",
                )},
                "approval_id": approval["id"], "grant_id": grant["id"],  # type: ignore[index]
                "command_id": steps[0]["command_id"], "execution_id": terminal["id"],  # type: ignore[index]
                "execution_status": terminal["status"], "recovery_status": recovery["status"],
                "resolution": resolution,
                "report_review": report["draft"], "report_v1": self._report_v1(scope),
                "destination": prepared["destination"],
            }
        except (KeyError, TypeError, ValueError, RuntimeError):
            return None

    def publish(
        self, scope: RerunScope, executed: dict[str, object], narrative: dict[str, str],
        *, operation_id: str, sre_username: str, sre_password: str,
    ) -> dict[str, object]:
        browser = self.console.publish_v08(
            base_url=self.base_url, username=sre_username, password=sre_password,
            incident_id=scope.incident_id, narrative=narrative,
        )
        self._browser(browser, None, [
            f"/api/v1/incidents/{scope.incident_id}/report",
            f"/api/v1/incidents/{scope.incident_id}/report/publish",
        ])
        return self._published(scope, executed, operation_id)

    def reconcile_publish(
        self, scope: RerunScope, executed: dict[str, object], _narrative: dict[str, str],
        *, operation_id: str,
    ) -> dict[str, object] | None:
        try:
            return self._published(scope, executed, operation_id, attempts=1)
        except (KeyError, TypeError, ValueError, RuntimeError, TimeoutError):
            return None

    def _public_signal(
        self, scope: RerunScope, trigger: dict[str, object],
    ) -> dict[str, object]:
        run_id = str(trigger["run_id"])
        started_at = float(trigger["trigger_started_at"])
        for attempt in range(self.attempts):
            if self.now() > started_at + 180:
                break
            observed = self.telemetry.probe_v02(run_id)
            if v02_ready(
                observed, run_id=run_id, trigger_started_at=started_at,
                telemetry_deadline_at=started_at + 120,
            ) and self.now() <= started_at + 180:
                body = self._request(f"/api/v1/incidents/{scope.incident_id}/workbench")
                public = v02_public_fact(
                    body, incident_id=scope.incident_id,
                    fingerprints=signal_fingerprints(observed),
                    trigger_started_at=started_at, public_deadline_at=started_at + 180,
                )
                if public is not None:
                    incident = public.get("incident")
                    report_v1 = scope.report_v1 or {}
                    try:
                        reopened_at = float(incident["reopened_at"])  # type: ignore[index]
                        source_resolved_at = float(report_v1["source_resolved_at"])
                    except (KeyError, TypeError, ValueError) as exc:
                        raise ValueError("V08 reopen window facts are missing") from exc
                    if (
                        not isinstance(incident, dict)
                        or incident.get("lifecycle_state") != "reopened"
                        or not source_resolved_at <= reopened_at <= source_resolved_at + 86_400
                        or reopened_at < started_at
                    ):
                        raise ValueError("V08 signal did not reopen the same Incident in 24h")
                    return public
            if attempt + 1 < self.attempts:
                self.sleep(2)
        raise TimeoutError("V08 public second Alert did not reopen the first Incident")

    def _diagnosis(
        self, scope: RerunScope, investigation_id: str, run_id: str, fingerprint: str,
    ) -> dict[str, object]:
        for attempt in range(self.attempts):
            workbench = self._request(f"/api/v1/incidents/{scope.incident_id}/workbench")
            platform = self._request("/api/v1/platform/status")
            model = platform.get("capabilities", {}).get("model")
            decision = select_v03_action(
                workbench, incident_id=scope.incident_id,
                investigation_id=investigation_id, alert_fingerprint=fingerprint,
                model=model, now=self.now(),
            )
            if decision is not None:
                return {
                    "run_id": run_id, "recommended_action": decision.action,
                    "evidence_steps": workbench.get("evidence_steps"),
                }
            if attempt + 1 < self.attempts:
                self.sleep(2)
        raise TimeoutError("V08 second Diagnosis did not complete")

    def _change_review(self, change_request_id: str) -> tuple[dict[str, object], dict[str, object]]:
        for attempt in range(self.attempts):
            detail = self._request(f"/api/v1/change-requests/{change_request_id}").get("change_request")
            if isinstance(detail, dict) and detail.get("status") == "awaiting_approval":
                review = self._request(
                    f"/api/v1/change-requests/{change_request_id}/phase-approval",
                ).get("phase_review")
                changes = review.get("changes") if isinstance(review, dict) else None
                if isinstance(changes, list) and len(changes) == 1 and isinstance(changes[0], dict):
                    return detail, review
            if attempt + 1 < self.attempts:
                self.sleep(2)
        raise TimeoutError("V08 second Change did not reach exact approval review")

    def _terminal_execution(
        self, change_request_id: str, *, attempts: int | None = None,
    ) -> dict[str, object]:
        for attempt in range(attempts or self.attempts):
            value = self._request(
                f"/api/v1/change-requests/{change_request_id}/phase-execution",
            ).get("phase_execution")
            if isinstance(value, dict) and value.get("status") == "succeeded":
                return value
            if isinstance(value, dict) and value.get("status") not in {"queued", "dispatched", "started"}:
                raise ValueError("V08 second execution became non-success terminal")
            if attempt + 1 < (attempts or self.attempts):
                self.sleep(2)
        raise TimeoutError("V08 second execution did not succeed")

    def _resolved_recovery(
        self, scope: RerunScope, prepared: dict[str, object], terminal: dict[str, object],
        *, attempts: int | None = None,
    ) -> dict[str, object]:
        for attempt in range(attempts or self.attempts):
            observed = self.telemetry.probe_v06(
                str(prepared["run_id"]), str(prepared["alert_fingerprint"]),
            )
            workbench = self._request(f"/api/v1/incidents/{scope.incident_id}/workbench")
            incident = workbench.get("incident")
            investigation = workbench.get("investigation")
            recovery = workbench.get("recovery")
            signals = workbench.get("alert_signals")
            matches = [
                item for item in signals if isinstance(item, dict)
                and item.get("fingerprint") == prepared["alert_fingerprint"]
            ] if isinstance(signals, list) else []
            if (
                observed.get("run_id") == prepared["run_id"]
                and observed.get("alert_fingerprint") == prepared["alert_fingerprint"]
                and isinstance(incident, dict) and incident.get("status") == "resolved"
                and isinstance(investigation, dict)
                and investigation.get("id") == prepared["investigation_id"]
                and isinstance(recovery, dict) and recovery.get("status") == "resolved"
                and terminal.get("status") == "succeeded"
                and len(matches) == 1
                and self._stable_recovery(incident, recovery, matches[0], observed, terminal)
            ):
                return recovery
            if attempt + 1 < (attempts or self.attempts):
                self.sleep(2)
        raise TimeoutError("V08 second recovery did not resolve after stabilization")

    def _ready_report(self, scope: RerunScope, investigation_id: str) -> dict[str, object]:
        body = self._request(f"/api/v1/incidents/{scope.incident_id}/report")
        draft = body.get("draft")
        if (
            body.get("availability") != "ready"
            or not isinstance(draft, dict) or draft.get("status") != "draft"
            or investigation_id not in draft.get("included_investigation_ids", [])
        ):
            raise ValueError("V08 Report v2 draft is not ready")
        return body

    def _report_v1(self, scope: RerunScope) -> dict[str, object]:
        publications = self._request(
            f"/api/v1/incidents/{scope.incident_id}/report",
        ).get("publications")
        matches = [
            item for item in publications if isinstance(item, dict)
            and isinstance(scope.report_v1, dict) and item.get("id") == scope.report_v1.get("id")
        ] if isinstance(publications, list) else []
        if len(matches) != 1 or matches[0] != scope.report_v1:
            raise ValueError("V08 Report v1 public content changed")
        return matches[0]

    def _destination_receipt(
        self, scope: RerunScope, *,
        platform_admin_username: str, platform_admin_password: str,
    ) -> tuple[dict[str, object], dict[str, object] | None]:
        response = self.notification_admin.request(
            "GET", "/api/v1/admin/notification-destinations",
        )
        destinations = response.body.get("destinations") if response.status == 200 else None
        old = scope.destination or {}
        matches = [
            item for item in destinations if isinstance(item, dict)
            and item.get("id") == old.get("id")
        ] if isinstance(destinations, list) else []
        if len(matches) != 1:
            raise ValueError("V08 exact Notification Destination is unavailable")
        current = matches[0]
        revision = current.get("configuration_revision")
        if not isinstance(revision, str) or not revision:
            raise ValueError("V08 current Destination revision is unavailable")
        if revision == old.get("revision"):
            return dict(old), None
        name = current.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("V08 current Destination name is unavailable")
        destination = {"id": current["id"], "revision": revision}
        browser = self.console.verify_v08_destination(
            base_url=self.base_url,
            username=platform_admin_username,
            password=platform_admin_password,
            destination_id=str(current["id"]),
            destination_name=name,
            destination_revision=revision,
        )
        path = f"/api/v1/admin/notification-destinations/{current['id']}/test"
        self._browser(browser, "v08_destination_receipt", [path])
        verification = browser.summary.get("verification")
        if (
            not isinstance(verification, dict)
            or verification.get("revision") != revision
            or not verification.get("delivery_id")
        ):
            raise ValueError("V08 Destination test identity is incomplete")
        delivery = self._test_delivery(
            str(verification["delivery_id"]), destination,
        )
        return destination, {
            "destination_id": destination["id"],
            "revision": destination["revision"],
            "delivery_id": delivery["id"],
            "status": delivery["status"],
            "attempt_count": delivery.get("attempt_count"),
            "attempt_ids": self._attempt_ids(delivery),
            "provider_identity": delivery["provider_identity"],
        }

    def _test_delivery(
        self, delivery_id: str, destination: dict[str, object],
    ) -> dict[str, object]:
        for attempt in range(self.attempts):
            response = self.notification_admin.request(
                "GET", "/api/v1/admin/notification-deliveries",
            )
            deliveries = response.body.get("deliveries") if response.status == 200 else None
            matches = [
                item for item in deliveries if isinstance(item, dict)
                and item.get("id") == delivery_id
            ] if isinstance(deliveries, list) else []
            if len(matches) > 1:
                raise ValueError("V08 Destination test Delivery identity is ambiguous")
            if len(matches) == 1:
                delivery = matches[0]
                if delivery.get("status") in {"failed", "dead_letter", "suppressed"}:
                    raise ValueError("V08 changed Destination receipt Delivery failed")
                if delivery.get("status") == "sent":
                    if (
                        delivery.get("is_test") is not True
                        or delivery.get("destination_id") != destination["id"]
                        or delivery.get("destination_revision") != destination["revision"]
                        or not delivery.get("provider_identity")
                    ):
                        raise ValueError("V08 changed Destination receipt is not exact")
                    self._attempt_ids(delivery)
                    return delivery
            if attempt + 1 < self.attempts:
                self.sleep(2)
        raise TimeoutError("V08 changed Destination receipt did not reach sent")

    @staticmethod
    def _resolution_identity(
        scope: RerunScope, prepared: dict[str, object], recovery: dict[str, object],
    ) -> dict[str, object]:
        identity = {
            "incident_id": scope.incident_id,
            "alert_fingerprint": prepared.get("alert_fingerprint"),
            "recovery_observation_id": recovery.get("id"),
            "resolved_webhook_request_id": recovery.get("resolved_webhook_request_id"),
        }
        if any(not isinstance(value, str) or not value for value in identity.values()):
            raise ValueError("V08 second resolution identity is incomplete")
        return identity

    @staticmethod
    def _attempt_ids(delivery: dict[str, object]) -> list[str]:
        attempts = delivery.get("attempts")
        attempt_ids = [
            str(item.get("id") or "") for item in attempts if isinstance(item, dict)
        ] if isinstance(attempts, list) else []
        if not attempt_ids or any(not item for item in attempt_ids):
            raise ValueError("V08 Destination receipt lacks durable attempt identities")
        return attempt_ids

    def _published(
        self, scope: RerunScope, executed: dict[str, object], operation_id: str,
        *, attempts: int | None = None,
    ) -> dict[str, object]:
        body = self._request(f"/api/v1/incidents/{scope.incident_id}/report")
        publications = body.get("publications")
        if not isinstance(publications, list) or len(publications) != 2:
            raise ValueError("V08 public Report history is not exactly v1 plus v2")
        report_v1 = self._report_v1(scope)
        candidates = [item for item in publications if item != report_v1]
        if len(candidates) != 1 or not isinstance(candidates[0], dict):
            raise ValueError("V08 Report v2 projection is ambiguous")
        report_v2 = candidates[0]
        resolution = executed.get("resolution")
        if not isinstance(resolution, dict):
            raise ValueError("V08 second resolution identity is unavailable")
        for attempt in range(attempts or self.attempts):
            response = self.notification_admin.request(
                "GET", "/api/v1/admin/notification-deliveries",
            )
            deliveries = response.body.get("deliveries") if response.status == 200 else None
            matches = [
                item for item in deliveries if isinstance(item, dict)
                and item.get("id") != scope.old_delivery_id
                and item.get("is_test") is False
                and self._matches_resolution(item, resolution)
            ] if isinstance(deliveries, list) else []
            if len(matches) == 1:
                delivery = matches[0]
                destination = scope.destination or {}
                if (
                    delivery.get("destination_id") != destination.get("id")
                    or delivery.get("destination_revision") != destination.get("revision")
                ):
                    raise ValueError("V08 second Delivery changed Destination revision")
                if delivery.get("status") in {"failed", "dead_letter", "suppressed"}:
                    raise ValueError("V08 second resolved Delivery failed")
                if delivery.get("status") != "sent":
                    if attempt + 1 < (attempts or self.attempts):
                        self.sleep(2)
                    continue
                return {
                    "status": "succeeded", "operation_id": operation_id,
                    "run_id": executed["run_id"], "incident_id": scope.incident_id,
                    "investigation_id": executed["investigation_id"],
                    "report_v1": report_v1, "report_v2": report_v2,
                    "notification_delivery": delivery,
                }
            if len(matches) > 1:
                raise ValueError("V08 resolved event produced multiple second Deliveries")
            if attempt + 1 < (attempts or self.attempts):
                self.sleep(2)
        raise TimeoutError("V08 second resolved Delivery did not reach sent")

    @staticmethod
    def _matches_resolution(
        delivery: dict[str, object], resolution: dict[str, object],
    ) -> bool:
        request = delivery.get("request")
        subject = request.get("subject") if isinstance(request, dict) else None
        facts = request.get("facts") if isinstance(request, dict) else None
        return (
            isinstance(request, dict)
            and request.get("event_id") == delivery.get("event_id")
            and request.get("event_type") == "incident.resolved"
            and isinstance(subject, dict)
            and subject.get("type") == "incident"
            and subject.get("id") == resolution.get("incident_id")
            and isinstance(facts, dict)
            and facts.get("incident_id") == resolution.get("incident_id")
            and facts.get("recovery_observation_id")
            == resolution.get("recovery_observation_id")
            and facts.get("resolved_webhook_request_id")
            == resolution.get("resolved_webhook_request_id")
        )

    def _request(self, path: str) -> dict[str, object]:
        response = self.user.request("GET", path)
        if response.status != 200 or not isinstance(response.body, dict):
            raise RuntimeError(f"V08 public read failed: {path}")
        return response.body

    def _browser(
        self, result: Any, action: str | None, paths: list[str], *, allow_retry: bool = False,
    ) -> None:
        parsed = urlsplit(self.base_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        mutations = result.summary.get("mutations")
        actual = [item.get("path") for item in mutations if isinstance(item, dict)] \
            if isinstance(mutations, list) else []
        if (
            (action is not None and result.summary.get("action") != action)
            or result.summary.get("same_origin") is not True
            or any(item != origin for item in result.summary.get("origins", []))
            or result.summary.get("screenshots_masked") is not True
            or not (actual == paths or (
                allow_retry and len(actual) == 2 and actual[0] == paths[0]
                and actual[1].endswith("/retry")
            ))
        ):
            raise ValueError("V08 Console evidence is not exact")

    @staticmethod
    def _typed_success(step: dict[str, object]) -> bool:
        result = step.get("result")
        execution = result.get("execution") if isinstance(result, dict) else None
        checks = execution.get("post_checks") if isinstance(execution, dict) else None
        return (
            step.get("direction") == "forward"
            and step.get("status") == "succeeded"
            and isinstance(checks, list) and bool(checks)
            and all(isinstance(item, dict) and item.get("status") == "succeeded" for item in checks)
        )

    @staticmethod
    def _stable_recovery(
        incident: dict[str, object], recovery: dict[str, object], signal: dict[str, object],
        observed: dict[str, object], terminal: dict[str, object],
    ) -> bool:
        try:
            recovery_observed_at = float(recovery["observed_at"])
            stabilizes_at = float(recovery["stabilizes_at"])
            resolved_at = float(recovery["resolved_at"])
            observed_at = float(observed["observed_at"])
            metric_at = float(observed["recovery_metric_observed_at"])
            log_at = float(observed["recovery_log_observed_at"])
            started_at = float(terminal["started_at"])
            request_id = str(signal.get("recovered_webhook_request_id") or "")
            return (
                all(math.isfinite(value) for value in (
                    recovery_observed_at, stabilizes_at, resolved_at,
                    observed_at, metric_at, log_at, started_at,
                ))
                and bool(request_id)
                and recovery.get("resolved_webhook_request_id") == request_id
                and float(signal["updated_at"]) <= recovery_observed_at
                and stabilizes_at - recovery_observed_at >= 300
                and resolved_at == stabilizes_at == float(incident["resolved_at"])
                and observed_at >= stabilizes_at
                and metric_at >= started_at and log_at >= started_at
                and observed.get("recovery_metric_zero") is True
                and int(observed.get("recovery_log_lines", 0)) >= 1
                and observed.get("prometheus_alert_firing") is False
                and observed.get("alertmanager_alert_active") is False
            )
        except (KeyError, TypeError, ValueError):
            return False

    @staticmethod
    def _denial(result: Any, change_request_id: str) -> None:
        denials = result.summary.get("denials")
        mutations = result.summary.get("mutations")
        root = f"/api/v1/change-requests/{change_request_id}"
        expected = [
            ("GET", f"{root}/phase-approval"),
            ("POST", f"{root}/phase-approval/approve"),
            ("POST", f"{root}/phase-execution/start"),
        ]
        request_ids = [
            item.get("request_id") for item in denials if isinstance(item, dict)
        ] if isinstance(denials, list) else []
        if (
            result.summary.get("action") != "v08_denial"
            or any(result.summary.get(field) is not False for field in (
                "phase_review_visible", "approval_control_visible", "execution_control_visible",
            ))
            or not isinstance(denials, list) or len(denials) != 3
            or not isinstance(mutations, list) or len(mutations) != 2
            or len(request_ids) != len(set(request_ids))
            or any(
                not isinstance(item, dict) or item.get("method") != method
                or item.get("path") != path or item.get("status") != 404
                or item.get("error_code") != "not_found"
                or item.get("response_request_id") != item.get("request_id")
                or item.get("payload_keys") != ["error", "request_id", "service", "status"]
                or item.get("error_keys") != ["code", "message"]
                for item, (method, path) in zip(denials, expected, strict=True)
            )
            or any(
                not isinstance(mutation, dict)
                or any(
                    mutation.get(field) != denial.get(field)
                    for field in (
                        "request_id", "method", "path", "status",
                        "response_request_id", "error_code",
                    )
                )
                or mutation.get("identities") != {}
                for mutation, denial in zip(mutations, denials[1:], strict=True)
            )
        ):
            raise ValueError("V08 no-Authority denial is incomplete")
