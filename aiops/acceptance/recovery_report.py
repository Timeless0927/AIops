"""Recovery and Report gates for the First Run Module."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Protocol

from .evidence_types import Artifact, GateExecution
from .governed_change import GovernedChangeGateRunner
from .integration_support import fail_gate
from .verification_trigger import UserSession


class RecoveryProbe(Protocol):
    def probe_v06(self, run_id: str, alert_fingerprint: str) -> dict[str, object]: ...


class RecoveryReportGateRunner(GovernedChangeGateRunner):
    def run_v06(
        self,
        *,
        run_id: str,
        incident_id: str,
        investigation_id: str,
        alert_fingerprint: str,
        attempts: int = 120,
        deadline_seconds: float = 600,
    ) -> dict[str, str]:
        started_at = self.evidence.start_gate("V06")
        execution = self.evidence.resume_gate("V06")
        artifacts: list[Artifact] = []
        try:
            if (
                not all((run_id, incident_id, investigation_id, alert_fingerprint))
                or attempts < 1
                or not math.isfinite(deadline_seconds)
                or deadline_seconds <= 0
            ):
                raise ValueError("V06 requires exact run identities and a bounded deadline")
            wall_started_at = self.now()
            monotonic_deadline = self.monotonic() + deadline_seconds
            if not math.isfinite(wall_started_at) or not math.isfinite(monotonic_deadline):
                raise ValueError("V06 runner clocks are invalid")
            v05_artifact = self.evidence.passed_artifact_json(
                "V05", "approval-and-execution.json",
            )
            v05_value = v05_artifact["value"]
            execution_value = v05_value.get("execution")
            if (
                v05_value.get("run_id") != run_id
                or not isinstance(execution_value, dict)
                or execution_value.get("completed_at") is None
            ):
                raise ValueError("V06 is not bound to the exact terminal V05 execution")
            change_completed_at = float(execution_value["completed_at"])
            if not math.isfinite(change_completed_at):
                raise ValueError("V06 terminal V05 timestamp is invalid")
            intent = {
                "run_id": run_id,
                "incident_id": incident_id,
                "investigation_id": investigation_id,
                "alert_fingerprint": alert_fingerprint,
                "change_completed_at": change_completed_at,
                "v05_artifact_sha256": v05_artifact["sha256"],
                "deadline_at": wall_started_at + deadline_seconds,
            }
            artifacts.append(self.evidence.write_json("V06", "intent.json", intent))
            return self._complete_v06(
                intent=intent, artifacts=artifacts, started_at=started_at,
                attempts=attempts, execution=execution,
                monotonic_deadline=monotonic_deadline,
            )
        except Exception as exc:
            fail_gate(self.evidence, "V06", artifacts, exc, (), started_at)

    def resume_v06(self, *, attempts: int = 1) -> dict[str, str]:
        execution = self.evidence.resume_gate("V06")
        artifacts = list(execution.artifacts)
        try:
            retained = self._optional_artifact_json(artifacts, "recovery.json")
            if retained is not None:
                return self._record_v06(
                    retained, artifacts, execution.started_at, execution,
                )
            intent = self._artifact_json(artifacts, "intent.json")
            if attempts != 1:
                raise ValueError("V06 resume permits one reconciliation read")
            return self._complete_v06(
                intent=intent,
                artifacts=artifacts,
                started_at=execution.started_at,
                attempts=1,
                execution=execution,
                monotonic_deadline=None,
            )
        except Exception as exc:
            fail_gate(self.evidence, "V06", artifacts, exc, (), execution.started_at)

    def _complete_v06(
        self,
        *,
        intent: dict[str, object],
        artifacts: list[Artifact],
        started_at: str,
        attempts: int,
        execution: GateExecution,
        monotonic_deadline: float | None,
    ) -> dict[str, str]:
        probe = getattr(self.telemetry, "probe_v06", None)
        if self.user is None or not callable(probe):
            raise ValueError("V06 requires User and Recovery telemetry Adapters")
        run_id = str(intent.get("run_id") or "")
        incident_id = str(intent.get("incident_id") or "")
        investigation_id = str(intent.get("investigation_id") or "")
        alert_fingerprint = str(intent.get("alert_fingerprint") or "")
        change_completed_at = float(intent.get("change_completed_at", 0))
        deadline_at = float(intent.get("deadline_at", 0))
        if (
            not all((run_id, incident_id, investigation_id, alert_fingerprint))
            or not math.isfinite(deadline_at)
            or not math.isfinite(change_completed_at)
            or (
                monotonic_deadline is not None
                and not math.isfinite(monotonic_deadline)
            )
            or (monotonic_deadline is None and attempts != 1)
        ):
            raise ValueError("V06 durable intent is invalid")
        for attempt in range(attempts):
            observed = probe(run_id, alert_fingerprint)
            workbench = self.user.request(
                "GET", f"/api/v1/incidents/{incident_id}/workbench",
            )
            if workbench.status != 200:
                raise RuntimeError(f"V06 Workbench returned HTTP {workbench.status}")
            accepted = self._v06_fact(
                observed,
                workbench.body,
                run_id=run_id,
                incident_id=incident_id,
                investigation_id=investigation_id,
                alert_fingerprint=alert_fingerprint,
                change_completed_at=change_completed_at,
                deadline_at=deadline_at,
            )
            if accepted is not None:
                artifacts.append(self.evidence.write_json("V06", "recovery.json", accepted))
                return self._record_v06(accepted, artifacts, started_at, execution)
            if attempt + 1 >= attempts:
                break
            assert monotonic_deadline is not None
            remaining = monotonic_deadline - self.monotonic()
            if remaining <= 0:
                break
            self.sleep(min(5.0, remaining))
        raise TimeoutError("V06 recovery and stabilization did not converge before its deadline")

    @classmethod
    def _v06_fact(
        cls,
        observed: dict[str, object],
        body: dict[str, object],
        *,
        run_id: str,
        incident_id: str,
        investigation_id: str,
        alert_fingerprint: str,
        change_completed_at: float,
        deadline_at: float,
    ) -> dict[str, object] | None:
        if observed.get("run_id") != run_id or observed.get("alert_fingerprint") != alert_fingerprint:
            raise ValueError("V06 telemetry identity does not match the exact run")
        if not cls._telemetry_ready(observed, not_before=change_completed_at):
            return None
        incident = body.get("incident")
        investigation = body.get("investigation")
        recovery = body.get("recovery_observation")
        signals = body.get("alert_signals")
        if not isinstance(signals, list):
            raise ValueError("V06 Workbench omitted Alert Signal projections")
        matching = [
            item for item in signals
            if isinstance(item, dict) and item.get("fingerprint") == alert_fingerprint
        ]
        if len(matching) != 1:
            raise ValueError("V06 did not resolve one exact Alert fingerprint")
        signal = matching[0]
        if (
            not isinstance(incident, dict)
            or incident.get("id") != incident_id
            or not isinstance(investigation, dict)
            or investigation.get("id") != investigation_id
        ):
            raise ValueError("V06 public Incident or Investigation identity drifted")
        if (
            incident.get("status") != "resolved"
            or incident.get("lifecycle_state") != "resolved"
            or investigation.get("status") not in {"completed", "failed", "terminated"}
            or signal.get("status") != "recovered"
            or not isinstance(recovery, dict)
            or recovery.get("resolved_at") is None
        ):
            return None
        if not cls._resolved_after_stabilization(
            incident, recovery, signal, observed_at=float(observed["observed_at"]),
        ):
            raise ValueError("V06 recovery correlation or stabilization is invalid")
        if float(incident["resolved_at"]) > deadline_at:
            raise TimeoutError("V06 product resolution completed after the original deadline")
        return {
            "run_id": run_id,
            "alert_fingerprint": alert_fingerprint,
            "telemetry": observed,
            "incident": incident,
            "investigation": investigation,
            "resolved_alert_signal": signal,
            "recovery_observation": recovery,
        }

    @staticmethod
    def _telemetry_ready(
        observed: dict[str, object], *, not_before: float,
    ) -> bool:
        hashes = observed.get("recovery_log_ref_hashes")
        try:
            observed_at = float(observed["observed_at"])
            metric_observed_at = float(observed["recovery_metric_observed_at"])
            log_observed_at = float(observed["recovery_log_observed_at"])
            return (
                math.isfinite(observed_at)
                and math.isfinite(metric_observed_at)
                and math.isfinite(log_observed_at)
                and not_before <= metric_observed_at <= observed_at
                and not_before <= log_observed_at <= observed_at
                and int(observed.get("recovery_metric_series", 0)) >= 1
                and observed.get("recovery_metric_zero") is True
                and int(observed.get("recovery_log_lines", 0)) >= 1
                and isinstance(hashes, list)
                and len(hashes) == int(observed["recovery_log_lines"])
                and len(set(hashes)) == len(hashes)
                and all(
                    isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
                    for value in hashes
                )
                and observed.get("prometheus_alert_firing") is False
                and observed.get("alertmanager_alert_active") is False
            )
        except (KeyError, TypeError, ValueError):
            return False

    @staticmethod
    def _resolved_after_stabilization(
        incident: dict[str, object],
        recovery: dict[str, object],
        signal: dict[str, object],
        *,
        observed_at: float,
    ) -> bool:
        try:
            recovery_observed_at = float(recovery["observed_at"])
            stabilizes_at = float(recovery["stabilizes_at"])
            resolved_at = float(recovery["resolved_at"])
            request_id = str(signal.get("recovered_webhook_request_id") or "")
            return (
                bool(request_id)
                and recovery.get("resolved_webhook_request_id") == request_id
                and float(signal["updated_at"]) <= recovery_observed_at
                and stabilizes_at - recovery_observed_at >= 300
                and resolved_at == stabilizes_at
                and float(incident["resolved_at"]) == stabilizes_at
                and observed_at >= stabilizes_at
            )
        except (KeyError, TypeError, ValueError):
            return False

    def _record_v06(
        self,
        value: dict[str, object],
        artifacts: list[Artifact],
        started_at: str,
        execution: GateExecution,
    ) -> dict[str, str]:
        incident = value.get("incident")
        investigation = value.get("investigation")
        recovery = value.get("recovery_observation")
        if not all(isinstance(item, dict) for item in (incident, investigation, recovery)):
            raise ValueError("V06 retained recovery artifact is invalid")
        if any(operation.get("kind") != "gate_execution" for operation in execution.operations):
            raise ValueError("V06 operation journal contains an unexpected effect")
        self._reconcile_unresolved(
            "V06",
            execution,
            outcome="succeeded",
            public_fact={
                "incident_id": incident["id"],  # type: ignore[index]
                "recovery_observation_id": recovery["id"],  # type: ignore[index]
                "resolved_at": incident["resolved_at"],  # type: ignore[index]
                "terminal": True,
            },
        )
        self.evidence.record_gate("V06", "passed", artifacts, started_at=started_at)
        return {
            "run_id": str(value["run_id"]),
            "incident_id": str(incident["id"]),  # type: ignore[index]
            "investigation_id": str(investigation["id"]),  # type: ignore[index]
            "alert_fingerprint": str(value["alert_fingerprint"]),
            "recovery_observation_id": str(recovery["id"]),  # type: ignore[index]
            "resolved_at": str(incident["resolved_at"]),  # type: ignore[index]
        }

    def run_v07(
        self,
        *,
        run_id: str,
        incident_id: str,
        investigation_id: str,
        destination_revision: str,
        narrative: dict[str, str],
    ) -> dict[str, str]:
        started_at = self.evidence.start_gate("V07")
        artifacts: list[Artifact] = []
        try:
            if self.user is None or not all((
                run_id, incident_id, investigation_id, destination_revision,
            )):
                raise ValueError("V07 requires exact run, Report and Destination identities")
            self._validate_narrative(narrative)
            destination_attestations = self.evidence.require_verified_attestation(
                "S04", role="platform_administrator",
            )
            receipt_artifact = self.evidence.passed_artifact_json(
                "S04", "receipt-review.json",
            )
            s04_artifact = self.evidence.passed_artifact_json(
                "S04", "sent-and-selected.json",
            )
            receipt_value = receipt_artifact["value"]
            s04_value = s04_artifact["value"]
            destination_id = str(s04_value.get("destination_id") or "")
            if (
                s04_value.get("status") != "sent"
                or s04_value.get("revision") != destination_revision
                or s04_value.get("route_revision") != destination_revision
                or s04_value.get("pilot_route_selected") is not True
                or not destination_id
                or receipt_value.get("status") != "sent"
                or receipt_value.get("destination_id") != destination_id
                or receipt_value.get("revision") != destination_revision
                or receipt_value.get("delivery_id") != s04_value.get("delivery_id")
                or receipt_value.get("attempt_ids") != s04_value.get("attempt_ids")
                or not receipt_value.get("attempt_ids")
                or receipt_value.get("provider_identity")
                != s04_value.get("provider_identity")
                or not receipt_value.get("provider_identity")
                or not any(
                    item.get("statement", {}).get("note")
                    == f"notification_receipt_sha256={receipt_artifact['sha256']}"
                    for item in destination_attestations
                )
            ):
                raise ValueError("V07 Destination revision is not bound to passed S04 evidence")
            draft = self._ready_report(incident_id, investigation_id)
            review = {
                "run_id": run_id,
                "incident_id": incident_id,
                "investigation_id": investigation_id,
                "report": {
                    "draft_id": draft["id"],
                    "source_revision": draft["source_revision"],
                    "source_resolved_at": draft["source_resolved_at"],
                    "included_investigation_ids": draft["included_investigation_ids"],
                    "narrative": narrative,
                },
                "destination": {
                    "id": destination_id,
                    "revision": destination_revision,
                    "s04_artifact_path": s04_artifact["path"],
                    "s04_artifact_sha256": s04_artifact["sha256"],
                    "s04_receipt_path": receipt_artifact["path"],
                    "s04_receipt_sha256": receipt_artifact["sha256"],
                },
                "s04_attestations": [item["statement"] for item in destination_attestations],
            }
            review_artifact = self.evidence.write_json("V07", "report-review.json", review)
            artifacts.append(review_artifact)
            artifacts.append(self.evidence.write_json("V07", "intent.json", {
                **review,
                "report_review_sha256": review_artifact.sha256,
            }))
            return {
                "run_id": run_id,
                "incident_id": incident_id,
                "status": "awaiting_attestation",
                "report_review_sha256": review_artifact.sha256,
            }
        except Exception as exc:
            fail_gate(self.evidence, "V07", artifacts, exc, (), started_at)

    def resume_v07(
        self,
        *,
        notification_admin: UserSession,
        sre_username: str = "",
        sre_password: str = "",
        attempts: int = 300,
    ) -> dict[str, str]:
        execution = self.evidence.resume_gate("V07")
        artifacts = list(execution.artifacts)
        secrets = (sre_password,)
        try:
            retained = self._optional_artifact_json(artifacts, "report-and-delivery.json")
            if retained is not None:
                return self._record_v07(
                    retained, artifacts, execution.started_at, execution,
                )
            intent = self._artifact_json(artifacts, "intent.json")
            self._require_report_attestation(intent)
            publication_id = self._publish_or_reconcile_v07(
                execution,
                intent=intent,
                sre_username=sre_username,
                sre_password=sre_password,
                artifacts=artifacts,
            )
            publication = self._published_report(intent, publication_id)
            destination = intent.get("destination")
            if not isinstance(destination, dict):
                raise ValueError("V07 Destination intent is invalid")
            delivery = self._resolved_delivery(
                notification_admin,
                incident_id=str(intent["incident_id"]),
                destination_id=str(destination["id"]),
                destination_revision=str(destination["revision"]),
                attempts=attempts,
            )
            report_sha256 = hashlib.sha256(
                json.dumps(
                    publication, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                ).encode()
            ).hexdigest()
            final = {
                "run_id": intent["run_id"],
                "incident_id": intent["incident_id"],
                "investigation_id": intent["investigation_id"],
                "report": publication,
                "report_sha256": report_sha256,
                "notification_delivery": delivery,
                "destination": destination,
                "report_review_sha256": intent["report_review_sha256"],
                "attestations": [
                    item["statement"]
                    for item in self.evidence.require_verified_attestation("V07", role="sre")
                ],
            }
            artifacts.append(self.evidence.write_json(
                "V07", "report-and-delivery.json", final, known_secrets=secrets,
            ))
            return self._record_v07(final, artifacts, execution.started_at, execution)
        except Exception as exc:
            fail_gate(self.evidence, "V07", artifacts, exc, secrets, execution.started_at)

    def _publish_or_reconcile_v07(
        self,
        execution: GateExecution,
        *,
        intent: dict[str, object],
        sre_username: str,
        sre_password: str,
        artifacts: list[Artifact],
    ) -> str:
        incident_id = str(intent["incident_id"])
        expected_paths = [
            f"/api/v1/incidents/{incident_id}/report",
            f"/api/v1/incidents/{incident_id}/report/publish",
        ]
        if any(
            operation.get("kind") == "console_mutation"
            for operation in execution.operations
        ):
            facts = self._proved_console_facts(execution)
            if [item.get("path") for item in facts] != expected_paths:
                raise ValueError("V07 interrupted Console mutation sequence is not exact")
            return self._fact_identity(facts, "publication.id")
        if not sre_username or not sre_password:
            raise ValueError("V07 Console publication requires the SRE credential")
        publish_v07 = getattr(self.console, "publish_v07", None)
        if not callable(publish_v07):
            raise ValueError("V07 requires the Report Console Adapter")
        report = intent.get("report")
        if not isinstance(report, dict) or not isinstance(report.get("narrative"), dict):
            raise ValueError("V07 Report intent is invalid")
        browser = publish_v07(
            base_url=self.base_url,
            username=sre_username,
            password=sre_password,
            incident_id=incident_id,
            narrative=report["narrative"],
        )
        self._verify_browser_origin(browser.summary, "V07")
        mutations = browser.summary.get("mutations")
        paths = [item.get("path") for item in mutations if isinstance(item, dict)] \
            if isinstance(mutations, list) else []
        publication = browser.summary.get("publication")
        if paths != expected_paths or not isinstance(publication, dict) or not publication.get("id"):
            raise ValueError("V07 Console did not publish the exact Report mutation sequence")
        artifacts.append(self.evidence.write_json(
            "V07", "console-publication.json", browser.summary,
            known_secrets=(sre_password,),
        ))
        artifacts.extend(
            self.evidence.write_bytes("V07", name, value)
            for name, value in sorted(browser.screenshots.items())
        )
        return str(publication["id"])

    def _ready_report(self, incident_id: str, investigation_id: str) -> dict[str, object]:
        response = self.user.request("GET", f"/api/v1/incidents/{incident_id}/report")
        draft = response.body.get("draft") if response.status == 200 else None
        if (
            response.status != 200
            or response.body.get("availability") != "ready"
            or response.body.get("publications") != []
            or not isinstance(draft, dict)
            or draft.get("incident_id") != incident_id
            or draft.get("status") != "draft"
            or investigation_id not in draft.get("included_investigation_ids", [])
            or draft.get("facts", {}).get("incident", {}).get("status") != "resolved"
            or not isinstance(draft.get("narrative"), dict)
            or any(str(value).strip() for value in draft["narrative"].values())
        ):
            raise ValueError("V07 Incident Report version 1 is not ready from the resolved run")
        return draft

    def _published_report(
        self, intent: dict[str, object], publication_id: str,
    ) -> dict[str, object]:
        incident_id = str(intent["incident_id"])
        response = self.user.request("GET", f"/api/v1/incidents/{incident_id}/report")
        publications = response.body.get("publications") if response.status == 200 else None
        if not isinstance(publications, list) or len(publications) != 1:
            raise ValueError("V07 public Report projection is not immutable version 1")
        publication = publications[0]
        report = intent.get("report")
        if (
            not isinstance(publication, dict)
            or not isinstance(report, dict)
            or publication.get("id") != publication_id
            or publication.get("draft_id") != report.get("draft_id")
            or publication.get("incident_id") != incident_id
            or publication.get("version") != 1
            or publication.get("status") != "published"
            or publication.get("source_revision") != report.get("source_revision")
            or publication.get("source_resolved_at") != report.get("source_resolved_at")
            or publication.get("included_investigation_ids")
            != report.get("included_investigation_ids")
            or publication.get("narrative") != report.get("narrative")
            or not publication.get("published_by")
            or publication.get("published_at") is None
        ):
            raise ValueError("V07 published Report changed from the attested summary")
        return publication

    def _resolved_delivery(
        self,
        admin: UserSession,
        *,
        incident_id: str,
        destination_id: str,
        destination_revision: str,
        attempts: int,
    ) -> dict[str, object]:
        if attempts < 1:
            raise ValueError("V07 Delivery attempts must be positive")
        prefix = f"incident.resolved:{incident_id}:"
        for attempt in range(attempts):
            response = admin.request("GET", "/api/v1/admin/notification-deliveries")
            if response.status != 200:
                raise RuntimeError(
                    f"V07 Notification Delivery read returned HTTP {response.status}"
                )
            candidates = [
                item for item in response.body.get("deliveries", [])
                if isinstance(item, dict)
                and str(item.get("event_id") or "").startswith(prefix)
                and item.get("is_test") is False
            ]
            if len(candidates) > 1:
                raise ValueError("V07 resolved event produced multiple Delivery projections")
            if candidates:
                delivery = candidates[0]
                if (
                    delivery.get("destination_id") != destination_id
                    or delivery.get("destination_revision") != destination_revision
                ):
                    raise ValueError("V07 resolved Delivery used the wrong Destination revision")
                if delivery.get("status") in {"failed", "dead_letter", "suppressed"}:
                    raise ValueError("V07 resolved Notification reached a blocking state")
                if delivery.get("status") == "sent":
                    self._validate_delivery(delivery, incident_id=incident_id)
                    return delivery
            if attempt + 1 < attempts:
                self.sleep(2)
        raise TimeoutError("V07 resolved Notification Delivery did not reach sent")

    @staticmethod
    def _validate_delivery(delivery: dict[str, object], *, incident_id: str) -> None:
        request = delivery.get("request")
        subject = request.get("subject") if isinstance(request, dict) else None
        attempts = delivery.get("attempts")
        event_id = str(delivery.get("event_id") or "")
        try:
            event_version = int(event_id.rsplit(":", 1)[1])
        except (IndexError, ValueError) as exc:
            raise ValueError("V07 resolved Delivery event identity is invalid") from exc
        if (
            not isinstance(request, dict)
            or request.get("event_id") != event_id
            or request.get("event_type") != "incident.resolved"
            or not isinstance(subject, dict)
            or subject.get("type") != "incident"
            or subject.get("id") != incident_id
            or subject.get("version") != event_version
            or not isinstance(delivery.get("request_id"), str)
            or not delivery.get("request_id")
            or not isinstance(delivery.get("provider_identity"), str)
            or not delivery.get("provider_identity")
            or not isinstance(attempts, list)
            or not RecoveryReportGateRunner._valid_attempt_chain(delivery, attempts)
        ):
            raise ValueError("V07 sent Delivery lacks typed request/provider correlation")

    @staticmethod
    def _valid_attempt_chain(
        delivery: dict[str, object], attempts: list[object],
    ) -> bool:
        delivery_id = delivery.get("id")
        attempt_count = delivery.get("attempt_count")
        redelivery_count = delivery.get("redelivery_count")
        if (
            not isinstance(delivery_id, str)
            or not delivery_id
            or not isinstance(attempt_count, int)
            or isinstance(attempt_count, bool)
            or attempt_count < 1
            or attempt_count > 3
            or len(attempts) != attempt_count
            or redelivery_count != 0
        ):
            return False
        previous_completed_at = -math.inf
        for expected, item in enumerate(attempts, 1):
            if not isinstance(item, dict):
                return False
            try:
                started_at = float(item["started_at"])
                completed_at = float(item["completed_at"])
            except (KeyError, TypeError, ValueError):
                return False
            expected_outcome = "sent" if expected == attempt_count else "failed"
            if (
                item.get("id") != f"{delivery_id}:0:{expected}"
                or item.get("attempt") != expected
                or item.get("redelivery") != 0
                or item.get("outcome") != expected_outcome
                or not math.isfinite(started_at)
                or not math.isfinite(completed_at)
                or started_at < previous_completed_at
                or completed_at < started_at
            ):
                return False
            previous_completed_at = completed_at
        return True

    def _require_report_attestation(self, intent: dict[str, object]) -> None:
        expected_note = f"report_review_sha256={intent.get('report_review_sha256')}"
        attestations = self.evidence.require_verified_attestation("V07", role="sre")
        if not any(item.get("statement", {}).get("note") == expected_note for item in attestations):
            raise ValueError("V07 SRE attestation does not bind the Report review artifact")

    def _record_v07(
        self,
        value: dict[str, object],
        artifacts: list[Artifact],
        started_at: str,
        execution: GateExecution,
    ) -> dict[str, str]:
        report = value.get("report")
        delivery = value.get("notification_delivery")
        destination = value.get("destination")
        if not all(isinstance(item, dict) for item in (report, delivery, destination)):
            raise ValueError("V07 retained Report/Delivery artifact is invalid")
        self._reconcile_unresolved(
            "V07",
            execution,
            outcome="succeeded",
            public_fact={
                "report_publication_id": report["id"],  # type: ignore[index]
                "notification_delivery_id": delivery["id"],  # type: ignore[index]
                "terminal": True,
            },
        )
        self.evidence.record_gate("V07", "passed", artifacts, started_at=started_at)
        return {
            "run_id": str(value["run_id"]),
            "incident_id": str(value["incident_id"]),
            "investigation_id": str(value["investigation_id"]),
            "report_version": str(report["version"]),  # type: ignore[index]
            "report_publication_id": str(report["id"]),  # type: ignore[index]
            "report_sha256": str(value["report_sha256"]),
            "notification_delivery_id": str(delivery["id"]),  # type: ignore[index]
            "destination_revision": str(destination["revision"]),  # type: ignore[index]
        }

    @staticmethod
    def _validate_narrative(narrative: dict[str, str]) -> None:
        if set(narrative) != {"impact", "root_cause", "resolution_summary", "follow_up"} or any(
            not isinstance(value, str) or not value.strip() or len(value) > 20_000
            for value in narrative.values()
        ):
            raise ValueError("V07 Report narrative is incomplete or unbounded")

    @staticmethod
    def _optional_artifact_json(
        artifacts: list[Artifact], name: str,
    ) -> dict[str, object] | None:
        matches = [artifact for artifact in artifacts if artifact.path.name.endswith(name)]
        if not matches:
            return None
        if len(matches) != 1:
            raise ValueError(f"retained {name} artifact is not unique")
        return json.loads(matches[0].path.read_text())
