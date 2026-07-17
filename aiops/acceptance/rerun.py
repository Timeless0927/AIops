"""V08 second governance chain for the Rerun and Cleanup Module."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .evidence import AcceptanceEvidence, Artifact, GateExecution
from .integration_support import fail_gate
from .recovery_journal import RecoveryJournal
from .run_one_decisions import valid_run_id


@dataclass(frozen=True)
class RerunScope:
    release_root: Path
    old_run_id: str
    incident_id: str
    old_investigation_id: str = ""
    old_delivery_id: str = ""
    report_v1: dict[str, object] | None = None
    destination: dict[str, object] | None = None


@dataclass(frozen=True)
class RerunInputs:
    release_root: Path
    sre_username: str
    sre_password: str
    no_authority_username: str
    no_authority_password: str
    platform_admin_username: str
    platform_admin_password: str
    narrative: dict[str, str]


class RerunEffects(Protocol):
    def trigger(
        self, scope: RerunScope, *, operation_id: str,
    ) -> dict[str, object]: ...

    def reconcile_trigger(
        self, scope: RerunScope, *, operation_id: str,
    ) -> dict[str, object] | None: ...


class RerunChain(Protocol):
    def prepare(
        self, scope: RerunScope, trigger: dict[str, object], *, operation_id: str,
        sre_username: str, sre_password: str,
        no_authority_username: str, no_authority_password: str,
        platform_admin_username: str, platform_admin_password: str,
    ) -> dict[str, object]: ...

    def reconcile_prepare(
        self, scope: RerunScope, trigger: dict[str, object], *, operation_id: str,
    ) -> dict[str, object] | None: ...

    def execute(
        self, scope: RerunScope, prepared: dict[str, object], *, operation_id: str,
        sre_username: str, sre_password: str,
    ) -> dict[str, object]: ...

    def reconcile_execute(
        self, scope: RerunScope, prepared: dict[str, object], *, operation_id: str,
    ) -> dict[str, object] | None: ...

    def publish(
        self, scope: RerunScope, executed: dict[str, object], narrative: dict[str, str],
        *, operation_id: str, sre_username: str, sre_password: str,
    ) -> dict[str, object]: ...

    def reconcile_publish(
        self, scope: RerunScope, executed: dict[str, object], narrative: dict[str, str],
        *, operation_id: str,
    ) -> dict[str, object] | None: ...


class RerunGateRunner:
    """Advances the independent second chain inside the single V08 gate."""

    def __init__(
        self, *, evidence: AcceptanceEvidence, effects: RerunEffects, chain: RerunChain,
    ) -> None:
        self.evidence = evidence
        self.effects = effects
        self.chain = chain
        self.journal = RecoveryJournal(evidence)

    def run_v08(self, inputs: RerunInputs) -> dict[str, str]:
        started_at = self.evidence.start_gate("V08")
        execution = self.evidence.resume_gate("V08")
        artifacts: list[Artifact] = []
        secrets = (
            inputs.sre_password, inputs.no_authority_password, inputs.platform_admin_password,
        )
        try:
            self._validate_inputs(inputs)
            baseline = self._baseline()
            artifacts.extend([
                self.evidence.write_json("V08", "baseline.json", baseline),
                self.evidence.write_json("V08", "intent.json", {
                    "release_root": str(inputs.release_root),
                    "sre_username": inputs.sre_username,
                    "no_authority_username": inputs.no_authority_username,
                    "platform_admin_username": inputs.platform_admin_username,
                    "narrative": inputs.narrative,
                }),
            ])
            scope = self._scope(inputs, baseline)
            trigger = self._trigger(execution, artifacts, scope)
            prepared = self._prepare(execution, artifacts, scope, trigger, inputs)
            self._validate_prepared(prepared, baseline, trigger)
            return self._next_review(artifacts, baseline, prepared)
        except Exception as exc:
            fail_gate(self.evidence, "V08", artifacts, exc, secrets, started_at)

    def resume_v08(self, inputs: RerunInputs) -> dict[str, str]:
        execution = self.evidence.resume_gate("V08")
        artifacts = list(execution.artifacts)
        secrets = (
            inputs.sre_password, inputs.no_authority_password, inputs.platform_admin_password,
        )
        try:
            self._validate_inputs(inputs)
            intent = self.journal.artifact_json(artifacts, "intent.json")
            self._validate_resume_inputs(inputs, intent)
            baseline = self.journal.artifact_json(artifacts, "baseline.json")
            scope = self._scope(inputs, baseline)
            trigger = self._trigger(execution, artifacts, scope)
            prepared = self._prepare(execution, artifacts, scope, trigger, inputs)
            self._validate_prepared(prepared, baseline, trigger)
            if not any(item.path.name == "approval-review.json" for item in artifacts):
                receipt = next(
                    (item for item in artifacts if item.path.name == "destination-receipt-review.json"),
                    None,
                )
                if receipt is not None:
                    self.journal.require_bound_attestation(
                        "V08", role="platform_administrator",
                        note=f"notification_receipt_sha256={receipt.sha256}",
                    )
                return self._approval_review(artifacts, prepared)
            final = self.journal.optional_artifact_json(artifacts, "rerun-and-delivery.json")
            if final is not None:
                return self._record(final, artifacts, execution)
            executed = self.journal.optional_artifact_json(artifacts, "executed-chain.json")
            if executed is None:
                review = self.journal.artifact(artifacts, "approval-review.json")
                self.journal.require_bound_attestation(
                    "V08", role="sre", note=f"approval_review_sha256={review.sha256}",
                )
                executed = self._execute(execution, artifacts, scope, prepared, inputs)
                self._validate_executed(executed, baseline, prepared)
                report_review = self.evidence.write_json("V08", "report-review.json", {
                    "run_id": executed["run_id"],
                    "incident_id": executed["incident_id"],
                    "investigation_id": executed["investigation_id"],
                    "report": executed["report_review"],
                    "report_v1": baseline["report_v1"],
                    "destination": executed["destination"],
                    "narrative": intent["narrative"],
                })
                artifacts.append(report_review)
                return {
                    "gate_id": "V08", "status": "awaiting_attestation",
                    "role": "sre", "review_sha256": report_review.sha256,
                }
            self._validate_executed(executed, baseline, prepared)
            review = self.journal.artifact(artifacts, "report-review.json")
            self.journal.require_bound_attestation(
                "V08", role="sre", note=f"report_review_sha256={review.sha256}",
            )
            published = self._publish(
                execution, artifacts, scope, executed, dict(intent["narrative"]), inputs,
            )
            final = self._validate_published(published, baseline, executed, dict(intent["narrative"]))
            artifacts.append(self.evidence.write_json("V08", "rerun-and-delivery.json", final))
            return self._record(final, artifacts, execution)
        except Exception as exc:
            fail_gate(self.evidence, "V08", artifacts, exc, secrets, execution.started_at)

    def _trigger(
        self, execution: GateExecution, artifacts: list[Artifact], scope: RerunScope,
    ) -> dict[str, object]:
        operation_id = self.journal.operation_id("V08", execution.execution_id, "trigger")
        return self.journal.effect(
            "V08", execution, artifacts, operation_id=operation_id,
            kind="verification_job", artifact_name="triggered-run.json",
            dispatch=lambda: self.effects.trigger(scope, operation_id=operation_id),
            reconcile=lambda: self.effects.reconcile_trigger(scope, operation_id=operation_id),
        )

    def _prepare(
        self, execution: GateExecution, artifacts: list[Artifact], scope: RerunScope,
        trigger: dict[str, object], inputs: RerunInputs,
    ) -> dict[str, object]:
        operation_id = self.journal.operation_id("V08", execution.execution_id, "prepare")
        return self.journal.effect(
            "V08", execution, artifacts, operation_id=operation_id,
            kind="second_chain_prepare", artifact_name="prepared-chain.json",
            dispatch=lambda: self.chain.prepare(
                scope, trigger, operation_id=operation_id,
                sre_username=inputs.sre_username, sre_password=inputs.sre_password,
                no_authority_username=inputs.no_authority_username,
                no_authority_password=inputs.no_authority_password,
                platform_admin_username=inputs.platform_admin_username,
                platform_admin_password=inputs.platform_admin_password,
            ),
            reconcile=lambda: self.chain.reconcile_prepare(
                scope, trigger, operation_id=operation_id,
            ),
        )

    def _execute(
        self, execution: GateExecution, artifacts: list[Artifact], scope: RerunScope,
        prepared: dict[str, object], inputs: RerunInputs,
    ) -> dict[str, object]:
        operation_id = self.journal.operation_id("V08", execution.execution_id, "execute")
        return self.journal.effect(
            "V08", execution, artifacts, operation_id=operation_id,
            kind="second_chain_execute", artifact_name="executed-chain.json",
            dispatch=lambda: self.chain.execute(
                scope, prepared, operation_id=operation_id,
                sre_username=inputs.sre_username, sre_password=inputs.sre_password,
            ),
            reconcile=lambda: self.chain.reconcile_execute(
                scope, prepared, operation_id=operation_id,
            ),
        )

    def _publish(
        self, execution: GateExecution, artifacts: list[Artifact], scope: RerunScope,
        executed: dict[str, object], narrative: dict[str, str], inputs: RerunInputs,
    ) -> dict[str, object]:
        operation_id = self.journal.operation_id("V08", execution.execution_id, "publish")
        effective_scope = RerunScope(
            **{
                **scope.__dict__,
                "destination": dict(executed["destination"]),
            }
        )
        return self.journal.effect(
            "V08", execution, artifacts, operation_id=operation_id,
            kind="second_report_publish", artifact_name="published-chain.json",
            dispatch=lambda: self.chain.publish(
                effective_scope, executed, narrative, operation_id=operation_id,
                sre_username=inputs.sre_username, sre_password=inputs.sre_password,
            ),
            reconcile=lambda: self.chain.reconcile_publish(
                effective_scope, executed, narrative, operation_id=operation_id,
            ),
        )

    def _baseline(self) -> dict[str, object]:
        v03 = self.evidence.passed_artifact_json("V03", "diagnosis.json")["value"]
        v05 = self.evidence.passed_artifact_json("V05", "approval-and-execution.json")["value"]
        v07 = self.evidence.passed_artifact_json("V07", "report-and-delivery.json")["value"]
        execution = v05.get("execution") if isinstance(v05, dict) else None
        report = v07.get("report") if isinstance(v07, dict) else None
        delivery = v07.get("notification_delivery") if isinstance(v07, dict) else None
        destination = v07.get("destination") if isinstance(v07, dict) else None
        grant = execution.get("grant") if isinstance(execution, dict) else None
        steps = execution.get("steps") if isinstance(execution, dict) else None
        evidence_steps = v03.get("evidence_steps") if isinstance(v03, dict) else None
        action = v03.get("recommended_action") if isinstance(v03, dict) else None
        if not all(isinstance(item, dict) for item in (execution, report, delivery, destination, grant)):
            raise ValueError("V08 requires complete first-chain public facts")
        command_id = steps[0].get("command_id") if isinstance(steps, list) and len(steps) == 1 else None
        old_values = [
            v07.get("run_id"), v07.get("investigation_id"), report.get("id"),
            delivery.get("id"), execution.get("change_request_id"), execution.get("phase_id"),
            execution.get("revision_id"), execution.get("approval_id"), grant.get("id"),
            execution.get("id"), command_id,
            action.get("id") if isinstance(action, dict) else None,
            *(
                [item.get("id") for item in evidence_steps if isinstance(item, dict)]
                if isinstance(evidence_steps, list) and evidence_steps else [None]
            ),
        ]
        if (
            any(not isinstance(value, str) or not value for value in old_values)
            or len(old_values) != len(set(old_values))
            or not valid_run_id(v07.get("run_id"))
        ):
            raise ValueError("V08 first-chain identity inventory is incomplete or reused")
        old_ids = set(old_values)
        return {
            "old_run_id": v07["run_id"], "incident_id": v07["incident_id"],
            "old_investigation_id": v07["investigation_id"], "old_ids": sorted(old_ids),
            "old_delivery_id": delivery["id"],
            "report_v1": report, "report_v1_sha256": self._sha256(report),
            "destination": destination,
        }

    @staticmethod
    def _validate_prepared(
        value: dict[str, object], baseline: dict[str, object], trigger: dict[str, object],
    ) -> None:
        required_ids = [
            value.get("run_id"), value.get("investigation_id"),
            *(value.get("evidence_step_ids") if isinstance(value.get("evidence_step_ids"), list) else []),
            value.get("recommended_action_id"), value.get("change_request_id"),
            value.get("phase_id"), value.get("revision_id"),
        ]
        old_ids = set(baseline["old_ids"])
        if (
            value.get("status") != "succeeded"
            or value.get("run_id") != trigger.get("run_id")
            or value.get("run_id") == baseline.get("old_run_id")
            or not valid_run_id(value.get("run_id"))
            or value.get("incident_id") != baseline.get("incident_id")
            or value.get("investigation_id") == baseline.get("old_investigation_id")
            or not isinstance(value.get("investigation_sequence"), int)
            or int(value["investigation_sequence"]) != 2
            or len(required_ids) != len(set(required_ids))
            or any(not isinstance(item, str) or not item or item in old_ids for item in required_ids)
            or re.fullmatch(r"[0-9a-f]{64}", str(value.get("dry_run_hash") or "")) is None
            or not value.get("target_confirmation")
            or value.get("authorization_denials") != 3
            or value.get("grant_count") != 0 or value.get("command_count") != 0
            or not isinstance(value.get("approval_review"), dict)
            or value.get("report_v1") != baseline.get("report_v1")
            or not isinstance(value.get("destination"), dict)
            or value["destination"].get("id") != baseline["destination"].get("id")  # type: ignore[union-attr]
            or not value["destination"].get("revision")  # type: ignore[union-attr]
            or not RerunGateRunner._valid_receipt(value, baseline)
        ):
            raise ValueError("V08 preparation did not prove one independent second chain")

    @staticmethod
    def _validate_executed(
        value: dict[str, object], baseline: dict[str, object], prepared: dict[str, object],
    ) -> None:
        stable = ("run_id", "incident_id", "investigation_id", "change_request_id", "phase_id", "revision_id")
        new_ids = [value.get(key) for key in ("approval_id", "grant_id", "command_id", "execution_id")]
        resolution = value.get("resolution")
        if (
            value.get("status") != "succeeded"
            or any(value.get(key) != prepared.get(key) for key in stable)
            or value.get("execution_status") != "succeeded"
            or value.get("recovery_status") != "resolved"
            or any(not isinstance(item, str) or not item for item in new_ids)
            or len(new_ids) != len(set(new_ids))
            or any(item in set(baseline["old_ids"]) for item in new_ids)
            or not isinstance(value.get("report_review"), dict)
            or prepared.get("investigation_id") not in value["report_review"].get("included_investigation_ids", [])  # type: ignore[union-attr]
            or value.get("report_v1") != baseline.get("report_v1")
            or value.get("destination") != prepared.get("destination")
            or not isinstance(resolution, dict)
            or resolution.get("incident_id") != prepared.get("incident_id")
            or resolution.get("alert_fingerprint") != prepared.get("alert_fingerprint")
            or any(
                not isinstance(resolution.get(key), str) or not resolution.get(key)
                for key in ("recovery_observation_id", "resolved_webhook_request_id")
            )
        ):
            raise ValueError("V08 execution reused history or omitted resolved second-chain facts")

    @classmethod
    def _validate_published(
        cls, value: dict[str, object], baseline: dict[str, object],
        executed: dict[str, object], narrative: dict[str, str],
    ) -> dict[str, object]:
        report_v2 = value.get("report_v2")
        delivery = value.get("notification_delivery")
        destination = executed.get("destination")
        resolution = executed.get("resolution")
        report_v1 = baseline.get("report_v1")
        if not all(
            isinstance(item, dict)
            for item in (report_v2, delivery, destination, resolution, report_v1)
        ):
            raise ValueError("V08 publication facts are incomplete")
        if (
            value.get("status") != "succeeded"
            or any(value.get(key) != executed.get(key) for key in ("run_id", "incident_id", "investigation_id"))
            or value.get("report_v1") != report_v1
            or cls._sha256(value.get("report_v1")) != baseline.get("report_v1_sha256")
            or report_v2.get("id") in set(baseline["old_ids"])
            or report_v2.get("version") != int(report_v1.get("version", 0)) + 1
            or report_v2.get("status") != "published"
            or executed.get("investigation_id") not in report_v2.get("included_investigation_ids", [])
            or report_v2.get("narrative") != narrative
            or delivery.get("id") in set(baseline["old_ids"])
            or delivery.get("status") != "sent"
            or delivery.get("destination_id") != destination.get("id")
            or delivery.get("destination_revision") != destination.get("revision")
            or not cls._valid_second_delivery(delivery, resolution)
        ):
            raise ValueError("V08 Report v2 or resolved Delivery is not independent and exact")
        return {
            "run_id": value["run_id"], "incident_id": value["incident_id"],
            "investigation_id": value["investigation_id"],
            "identity_inventory": {
                key: executed[key] for key in (
                    "change_request_id", "phase_id", "revision_id", "approval_id",
                    "grant_id", "command_id", "execution_id",
                )
            },
            "report_v1_before": report_v1, "report_v1_after": value["report_v1"],
            "report_v1_sha256": baseline["report_v1_sha256"],
            "report_v2": report_v2, "notification_delivery": delivery,
            "destination": destination,
        }

    def _record(
        self, value: dict[str, object], artifacts: list[Artifact], execution: GateExecution,
    ) -> dict[str, str]:
        self.evidence.record_gate("V08", "passed", artifacts, started_at=execution.started_at)
        report = value["report_v2"]
        delivery = value["notification_delivery"]
        assert isinstance(report, dict) and isinstance(delivery, dict)
        return {
            "gate_id": "V08", "status": "passed", "run_id": str(value["run_id"]),
            "incident_id": str(value["incident_id"]),
            "investigation_id": str(value["investigation_id"]),
            "report_publication_id": str(report["id"]),
            "notification_delivery_id": str(delivery["id"]),
        }

    @staticmethod
    def _validate_inputs(inputs: RerunInputs) -> None:
        if (
            not inputs.release_root.is_absolute()
            or not all((
                inputs.sre_username, inputs.sre_password,
                inputs.no_authority_username, inputs.no_authority_password,
                inputs.platform_admin_username, inputs.platform_admin_password,
            ))
            or set(inputs.narrative) != {"impact", "root_cause", "resolution_summary", "follow_up"}
            or any(not isinstance(value, str) or not value.strip() or len(value) > 20_000 for value in inputs.narrative.values())
        ):
            raise ValueError("V08 inputs are incomplete or unbounded")

    @staticmethod
    def _validate_resume_inputs(inputs: RerunInputs, intent: dict[str, object]) -> None:
        if (
            intent.get("release_root") != str(inputs.release_root)
            or intent.get("sre_username") != inputs.sre_username
            or intent.get("no_authority_username") != inputs.no_authority_username
            or intent.get("platform_admin_username") != inputs.platform_admin_username
            or intent.get("narrative") != inputs.narrative
        ):
            raise ValueError("V08 resume inputs drifted from durable intent")

    @staticmethod
    def _scope(inputs: RerunInputs, baseline: dict[str, object]) -> RerunScope:
        return RerunScope(
            release_root=inputs.release_root,
            old_run_id=str(baseline["old_run_id"]),
            incident_id=str(baseline["incident_id"]),
            old_investigation_id=str(baseline["old_investigation_id"]),
            old_delivery_id=str(baseline["old_delivery_id"]),
            report_v1=dict(baseline["report_v1"]),
            destination=dict(baseline["destination"]),
        )

    @staticmethod
    def _sha256(value: object) -> str:
        return hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(),
        ).hexdigest()

    def _next_review(
        self, artifacts: list[Artifact], baseline: dict[str, object], prepared: dict[str, object],
    ) -> dict[str, str]:
        if prepared["destination"] != baseline["destination"]:
            receipt = self.evidence.write_json(
                "V08", "destination-receipt-review.json", prepared["receipt_review"],
            )
            artifacts.append(receipt)
            return {
                "gate_id": "V08", "status": "awaiting_attestation",
                "role": "platform_administrator", "review_sha256": receipt.sha256,
            }
        return self._approval_review(artifacts, prepared)

    def _approval_review(
        self, artifacts: list[Artifact], prepared: dict[str, object],
    ) -> dict[str, str]:
        review = self.evidence.write_json("V08", "approval-review.json", prepared)
        artifacts.append(review)
        return {
            "gate_id": "V08", "status": "awaiting_attestation",
            "role": "sre", "review_sha256": review.sha256,
        }

    @staticmethod
    def _valid_receipt(value: dict[str, object], baseline: dict[str, object]) -> bool:
        destination = value.get("destination")
        old = baseline.get("destination")
        if not isinstance(destination, dict) or not isinstance(old, dict):
            return False
        if destination == old:
            return value.get("receipt_review") is None
        receipt = value.get("receipt_review")
        attempt_ids = receipt.get("attempt_ids") if isinstance(receipt, dict) else None
        return (
            isinstance(receipt, dict)
            and receipt.get("status") == "sent"
            and receipt.get("destination_id") == destination.get("id")
            and receipt.get("revision") == destination.get("revision")
            and isinstance(receipt.get("delivery_id"), str)
            and bool(receipt["delivery_id"])
            and isinstance(receipt.get("provider_identity"), str)
            and bool(receipt["provider_identity"])
            and isinstance(attempt_ids, list)
            and bool(attempt_ids)
            and all(isinstance(item, str) and item for item in attempt_ids)
            and len(attempt_ids) == len(set(attempt_ids))
        )

    @staticmethod
    def _valid_second_delivery(
        delivery: dict[str, object], resolution: dict[str, object],
    ) -> bool:
        request = delivery.get("request")
        subject = request.get("subject") if isinstance(request, dict) else None
        facts = request.get("facts") if isinstance(request, dict) else None
        attempts = delivery.get("attempts")
        event_id = str(delivery.get("event_id") or "")
        try:
            event_version = int(event_id.rsplit(":", 1)[1])
        except (IndexError, ValueError):
            return False
        attempt_ids = [
            item.get("id") for item in attempts if isinstance(item, dict)
        ] if isinstance(attempts, list) else []
        return (
            delivery.get("is_test") is False
            and isinstance(delivery.get("request_id"), str)
            and bool(delivery["request_id"])
            and isinstance(delivery.get("provider_identity"), str)
            and bool(delivery["provider_identity"])
            and isinstance(request, dict)
            and request.get("event_id") == event_id
            and request.get("event_type") == "incident.resolved"
            and isinstance(subject, dict)
            and subject.get("type") == "incident"
            and subject.get("id") == resolution.get("incident_id")
            and subject.get("version") == event_version
            and isinstance(facts, dict)
            and facts.get("incident_id") == resolution.get("incident_id")
            and facts.get("status") == "resolved"
            and facts.get("recovery_observation_id")
            == resolution.get("recovery_observation_id")
            and facts.get("resolved_webhook_request_id")
            == resolution.get("resolved_webhook_request_id")
            and isinstance(attempts, list)
            and bool(attempt_ids)
            and all(isinstance(item, str) and item for item in attempt_ids)
            and len(attempt_ids) == len(set(attempt_ids))
            and delivery.get("attempt_count") == len(attempts)
        )
