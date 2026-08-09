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


def _ledger(
    tmp_path: Path,
    gate_id: str,
    *,
    s04_note_override: str | None = None,
    s04_receipt_overrides: dict[str, object] | None = None,
) -> AcceptanceLedger:
    evidence = create_evidence(
        tmp_path,
        acceptance_id=f"recovery-report-{gate_id.lower()}",
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
        if predecessor == "S04":
            receipt_value = {
                "status": "sent",
                "revision": "notification-revision-1",
                "destination_id": "destination-1",
                "delivery_id": "delivery-test-1",
                "provider_identity": "provider-test-1",
                "attempt_ids": ["attempt-test-1"],
            }
            receipt_value.update(s04_receipt_overrides or {})
            receipt = evidence.write_json("S04", "receipt-review.json", receipt_value)
            artifact = evidence.write_json("S04", "sent-and-selected.json", {
                "status": "sent",
                "revision": "notification-revision-1",
                "route_revision": "notification-revision-1",
                "destination_id": "destination-1",
                "delivery_id": "delivery-test-1",
                "provider_identity": "provider-test-1",
                "attempt_ids": ["attempt-test-1"],
                "pilot_route_selected": True,
            })
            evidence.record_gate("S04", GateResult("passed", tuple([receipt, artifact])))
            _attest(
                evidence, "S04", "platform_administrator",
                s04_note_override or f"notification_receipt_sha256={receipt.sha256}",
            )
        elif predecessor == "V05":
            artifact = evidence.write_json("V05", "approval-and-execution.json", {
                "run_id": "run-controller-uid-1",
                "execution": {"id": "execution-run-one", "completed_at": 1000.0},
            })
            evidence.record_gate("V05", GateResult("passed", tuple([artifact])))
        else:
            evidence.record_gate(predecessor, GateResult("not_applicable" if predecessor == "I04" else "passed", ()))
    return evidence


def _attest(
    evidence: AcceptanceLedger, gate_id: str, role: str, note: str,
) -> None:
    statement = evidence.attestation_statement(
        actor="Acceptance User",
        role=role,
        gate_ids=[gate_id],
        conclusion="passed",
        note=note,
    )
    evidence.append_attestation(
        statement,
        signature=f"signature-{gate_id}",
        public_key="ssh-ed25519 test",
        fingerprint="SHA256:test",
    )


class RecoveryTelemetry:
    def __init__(
        self, *, interrupt: bool = False, metric_at: float = 1310.0,
        log_at: float = 1305.0,
    ) -> None:
        self.interrupt = interrupt
        self.metric_at = metric_at
        self.log_at = log_at

    def probe_v06(self, run_id: str, alert_fingerprint: str) -> dict[str, object]:
        if self.interrupt:
            raise KeyboardInterrupt
        return {
            "run_id": run_id,
            "alert_fingerprint": alert_fingerprint,
            "observed_at": 1315.0,
            "recovery_metric_series": 1,
            "recovery_metric_zero": True,
            "recovery_metric_observed_at": self.metric_at,
            "recovery_log_lines": 1,
            "recovery_log_ref_hashes": ["d" * 64],
            "recovery_log_observed_at": self.log_at,
            "prometheus_alert_firing": False,
            "alertmanager_alert_active": False,
            "commands": [],
        }


class RecoveryUser:
    def __init__(
        self,
        *,
        stabilization_seconds: float = 300.0,
        resolved_webhook_request_id: str = "alertmanager-resolved-request-1",
        resolved_at: float = 1315.0,
    ) -> None:
        self.stabilization_seconds = stabilization_seconds
        self.resolved_webhook_request_id = resolved_webhook_request_id
        self.resolved_at = resolved_at

    def request(self, method: str, path: str, **_kwargs) -> HttpResponse:
        assert method == "GET"
        assert path == "/api/v1/incidents/incident-run-one/workbench"
        observed_at = 1315.0 - self.stabilization_seconds
        return HttpResponse(200, {
            "incident": {
                "id": "incident-run-one",
                "status": "resolved",
                "lifecycle_state": "resolved",
                "resolved_at": self.resolved_at,
            },
            "investigation": {"id": "investigation-run-one", "status": "completed"},
            "alert_signals": [{
                "fingerprint": "fingerprint-run-one",
                "status": "recovered",
                "updated_at": observed_at,
                "recovered_webhook_request_id": "alertmanager-resolved-request-1",
            }],
            "recovery_observation": {
                "id": "recovery-run-one",
                "observed_at": observed_at,
                "stabilizes_at": 1315.0,
                "resolved_at": self.resolved_at,
                "resolved_webhook_request_id": self.resolved_webhook_request_id,
            },
        }, {})


def _narrative() -> dict[str, str]:
    return {
        "impact": "The controlled verification workload was unavailable.",
        "root_cause": "The exact run latched its readiness failure.",
        "resolution_summary": "The approved rollout replaced the failed Pod.",
        "follow_up": "Retain this run as pilot acceptance evidence.",
    }


class ReportUser:
    def __init__(self, *, mutate_after_publish: bool = False) -> None:
        self.mutate_after_publish = mutate_after_publish
        self.publication: dict[str, object] | None = None
        self.draft = {
            "id": "report-draft-1",
            "incident_id": "incident-run-one",
            "source_revision": 7,
            "source_resolved_at": 1315.0,
            "included_investigation_ids": ["investigation-run-one"],
            "facts": {"incident": {"id": "incident-run-one", "status": "resolved"}},
            "decision_action_history": {"change_requests": []},
            "evidence_references": ["evidence-1"],
            "narrative": {key: "" for key in _narrative()},
            "status": "draft",
            "created_at": 1316.0,
            "updated_at": 1316.0,
        }

    def publish(self, narrative: dict[str, str]) -> dict[str, object]:
        self.publication = {
            **self.draft,
            "id": "report-publication-1",
            "draft_id": self.draft["id"],
            "narrative": dict(narrative),
            "status": "published",
            "version": 1,
            "published_by": "sre-1",
            "published_at": 1320.0,
        }
        return self.publication

    def request(self, method: str, path: str, **_kwargs) -> HttpResponse:
        assert method == "GET" and path == "/api/v1/incidents/incident-run-one/report"
        publications = [] if self.publication is None else [dict(self.publication)]
        if publications and self.mutate_after_publish:
            publications[0]["narrative"] = {**_narrative(), "impact": "changed"}
        return HttpResponse(200, {
            "availability": "ready",
            "draft": self.draft,
            "publications": publications,
        }, {})


class ReportConsole:
    def __init__(self, user: ReportUser) -> None:
        self.user = user
        self.actions: list[str] = []

    def publish_v07(self, **kwargs) -> BrowserResult:
        self.actions.append("v07")
        publication = self.user.publish(kwargs["narrative"])
        root = "/api/v1/incidents/incident-run-one/report"
        return BrowserResult({
            "same_origin": True,
            "origins": ["http://pilot.test"],
            "publication": publication,
            "mutations": [{"path": root}, {"path": f"{root}/publish"}],
        }, {"v07.png": b"png"})


class InterruptedReportConsole(ReportConsole):
    def __init__(self, user: ReportUser, evidence: AcceptanceLedger) -> None:
        super().__init__(user)
        self.evidence = evidence

    def publish_v07(self, **kwargs) -> BrowserResult:
        publication = self.user.publish(kwargs["narrative"])
        root = "/api/v1/incidents/incident-run-one/report"
        for index, fact in enumerate((
            {
                "path": root,
                "identities": {"draft.id": "report-draft-1", "draft.source_revision": 7},
            },
            {
                "path": f"{root}/publish",
                "identities": {
                    "publication.id": publication["id"],
                    "publication.source_revision": 7,
                },
            },
        ), 1):
            operation_id = f"request-v07-{index}"
            self.evidence.bind_operation(
                "V07", kind="console_mutation", operation_id=operation_id,
            )
            self.evidence.reconcile_operation(
                "V07",
                operation_id=operation_id,
                outcome="succeeded",
                public_fact={"request_id": operation_id, **fact},
            )
        raise KeyboardInterrupt


class NotificationAdmin:
    def __init__(
        self,
        *,
        status: str = "sent",
        destination_revision: str = "notification-revision-1",
        provider_identity: str | None = "provider-message-1",
        attempts: list[dict[str, object]] | None = None,
        redelivery_count: int = 0,
    ) -> None:
        self.status = status
        self.destination_revision = destination_revision
        self.provider_identity = provider_identity
        self.attempts = attempts
        self.redelivery_count = redelivery_count

    def request(self, method: str, path: str, **_kwargs) -> HttpResponse:
        assert method == "GET" and path == "/api/v1/admin/notification-deliveries"
        attempts = self.attempts
        if attempts is None:
            attempts = [{
                "id": "delivery-resolved-1:0:1",
                "attempt": 1,
                "redelivery": 0,
                "outcome": "sent",
                "started_at": 1316.0,
                "completed_at": 1317.0,
            }] if self.status == "sent" else []
        return HttpResponse(200, {"deliveries": [{
            "id": "delivery-resolved-1",
            "event_id": "incident.resolved:incident-run-one:7",
            "request_id": "gateway-outbox-request-1",
            "request": {
                "event_id": "incident.resolved:incident-run-one:7",
                "event_type": "incident.resolved",
                "subject": {"type": "incident", "id": "incident-run-one", "version": 7},
            },
            "provider_identity": self.provider_identity,
            "destination_id": "destination-1",
            "destination_revision": self.destination_revision,
            "status": self.status,
            "is_test": False,
            "redelivery_count": self.redelivery_count,
            "attempt_count": len(attempts),
            "attempts": attempts,
        }]}, {})


class UnusedCommands:
    def run(self, *_args, **_kwargs):
        raise AssertionError("recovery/report tests must not invoke shell commands")


def _runner(
    evidence: AcceptanceLedger,
    user: object,
    console: object,
    telemetry: object,
    *,
    now=lambda: 1000.0,
    monotonic=lambda: 0.0,
    sleep=lambda _seconds: None,
) -> RunOneGateRunner:
    return RunOneGateRunner(
        evidence=evidence,
        commands=UnusedCommands(),
        console=console,
        base_url="http://pilot.test",
        user=user,
        telemetry=telemetry,
        now=now,
        monotonic=monotonic,
        sleep=sleep,
    )


def test_v06_binds_fresh_telemetry_webhook_and_full_stabilization(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path, "V06")
    result = _runner(
        evidence, RecoveryUser(), object(), RecoveryTelemetry(),
    ).run_v06(
        run_id="run-controller-uid-1",
        incident_id="incident-run-one",
        investigation_id="investigation-run-one",
        alert_fingerprint="fingerprint-run-one",
        attempts=1,
    )
    assert result["recovery_observation_id"] == "recovery-run-one"
    retained = json.loads(next(tmp_path.rglob("recovery.json")).read_text())
    assert retained["resolved_alert_signal"]["recovered_webhook_request_id"] \
        == retained["recovery_observation"]["resolved_webhook_request_id"]
    assert retained["telemetry"]["recovery_metric_zero"] is True


@pytest.mark.parametrize(
    "user",
    [
        RecoveryUser(stabilization_seconds=299),
        RecoveryUser(resolved_webhook_request_id="wrong-request"),
        RecoveryUser(resolved_at=1700.0),
    ],
)
def test_v06_rejects_short_drifted_or_late_resolution(
    tmp_path: Path, user: RecoveryUser,
) -> None:
    evidence = _ledger(tmp_path, "V06")
    with pytest.raises(GateFailed, match="V06"):
        _runner(evidence, user, object(), RecoveryTelemetry()).run_v06(
            run_id="run-controller-uid-1",
            incident_id="incident-run-one",
            investigation_id="investigation-run-one",
            alert_fingerprint="fingerprint-run-one",
            attempts=1,
        )


def test_v06_resume_reuses_original_deadline_and_only_repeats_reads(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path, "V06")
    with pytest.raises(KeyboardInterrupt):
        _runner(
            evidence, RecoveryUser(), object(), RecoveryTelemetry(interrupt=True),
        ).run_v06(
            run_id="run-controller-uid-1",
            incident_id="incident-run-one",
            investigation_id="investigation-run-one",
            alert_fingerprint="fingerprint-run-one",
            attempts=1,
        )
    reopened = open_evidence(evidence.root)

    def monotonic_must_not_restart() -> float:
        raise AssertionError("V06 resume must not restart its monotonic budget")

    result = _runner(
        reopened,
        RecoveryUser(),
        object(),
        RecoveryTelemetry(),
        now=lambda: 1200.0,
        monotonic=monotonic_must_not_restart,
    ).resume_v06(attempts=1)
    assert result["resolved_at"] == "1315.0"


def test_v06_polling_budget_uses_monotonic_time_not_wall_clock(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path, "V06")
    wall = iter((1000.0, -1_000_000.0, 1_000_000.0))
    monotonic = [10.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        monotonic[0] += seconds

    with pytest.raises(GateFailed, match="V06"):
        _runner(
            evidence,
            RecoveryUser(),
            object(),
            RecoveryTelemetry(metric_at=999.0, log_at=999.0),
            now=lambda: next(wall),
            monotonic=lambda: monotonic[0],
            sleep=sleep,
        ).run_v06(
            run_id="run-controller-uid-1",
            incident_id="incident-run-one",
            investigation_id="investigation-run-one",
            alert_fingerprint="fingerprint-run-one",
            attempts=2,
            deadline_seconds=60,
        )
    assert sleeps == [5.0]


def test_v06_rejects_metric_or_log_older_than_terminal_v05(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path, "V06")
    with pytest.raises(GateFailed, match="V06"):
        _runner(
            evidence,
            RecoveryUser(),
            object(),
            RecoveryTelemetry(metric_at=999.0, log_at=999.0),
        ).run_v06(
            run_id="run-controller-uid-1",
            incident_id="incident-run-one",
            investigation_id="investigation-run-one",
            alert_fingerprint="fingerprint-run-one",
            attempts=1,
        )


def _prepare_v07(
    tmp_path: Path,
    *,
    user: ReportUser | None = None,
    console: ReportConsole | None = None,
) -> tuple[AcceptanceLedger, ReportUser, ReportConsole, dict[str, str]]:
    evidence = _ledger(tmp_path, "V07")
    user = user or ReportUser()
    console = console or ReportConsole(user)
    paused = _runner(evidence, user, console, RecoveryTelemetry()).run_v07(
        run_id="run-controller-uid-1",
        incident_id="incident-run-one",
        investigation_id="investigation-run-one",
        destination_revision="notification-revision-1",
        narrative=_narrative(),
    )
    assert paused["status"] == "awaiting_attestation"
    _attest(
        evidence,
        "V07",
        "sre",
        f"report_review_sha256={paused['report_review_sha256']}",
    )
    return evidence, user, console, paused


def test_v07_pauses_then_publishes_through_console_and_binds_delivery(tmp_path: Path) -> None:
    evidence, user, console, paused = _prepare_v07(tmp_path)
    result = _runner(evidence, user, console, RecoveryTelemetry()).resume_v07(
        notification_admin=NotificationAdmin(),
        sre_username="pilot-sre",
        sre_password="sre-password",
        attempts=1,
    )
    assert console.actions == ["v07"]
    assert result["report_version"] == "1"
    assert result["notification_delivery_id"] == "delivery-resolved-1"
    retained = json.loads(next(tmp_path.rglob("report-and-delivery.json")).read_text())
    assert retained["report_review_sha256"] == paused["report_review_sha256"]
    assert retained["destination"]["s04_artifact_sha256"]
    assert retained["notification_delivery"]["provider_identity"] == "provider-message-1"
    assert "sre-password" not in json.dumps(json.loads(evidence.manifest_path.read_text()))


@pytest.mark.parametrize(
    "admin",
    [
        NotificationAdmin(status="failed"),
        NotificationAdmin(status="dead_letter"),
        NotificationAdmin(status="suppressed"),
        NotificationAdmin(destination_revision="wrong-revision"),
        NotificationAdmin(provider_identity=None),
    ],
)
def test_v07_fails_closed_for_terminal_or_uncorrelated_delivery(
    tmp_path: Path, admin: NotificationAdmin,
) -> None:
    evidence, user, console, _paused = _prepare_v07(tmp_path)
    with pytest.raises(GateFailed, match="V07"):
        _runner(evidence, user, console, RecoveryTelemetry()).resume_v07(
            notification_admin=admin,
            sre_username="pilot-sre",
            sre_password="password",
            attempts=1,
        )


@pytest.mark.parametrize(
    "admin",
    [
        NotificationAdmin(attempts=[{
            "id": "wrong-attempt-id", "attempt": 1, "redelivery": 0,
            "outcome": "sent", "started_at": 1316.0, "completed_at": 1317.0,
        }]),
        NotificationAdmin(redelivery_count=1),
    ],
)
def test_v07_rejects_inexact_or_redelivered_attempt_chain(
    tmp_path: Path, admin: NotificationAdmin,
) -> None:
    evidence, user, console, _paused = _prepare_v07(tmp_path)
    with pytest.raises(GateFailed, match="V07"):
        _runner(evidence, user, console, RecoveryTelemetry()).resume_v07(
            notification_admin=admin,
            sre_username="pilot-sre",
            sre_password="password",
            attempts=1,
        )


def test_v07_rejects_publication_that_changes_after_console_publish(tmp_path: Path) -> None:
    user = ReportUser(mutate_after_publish=True)
    console = ReportConsole(user)
    evidence, _, _, _paused = _prepare_v07(tmp_path, user=user, console=console)
    with pytest.raises(GateFailed, match="V07"):
        _runner(evidence, user, console, RecoveryTelemetry()).resume_v07(
            notification_admin=NotificationAdmin(),
            sre_username="pilot-sre",
            sre_password="password",
            attempts=1,
        )


def test_v07_rejects_tampered_s04_destination_artifact(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path, "V07")
    s04 = next(tmp_path.rglob("sent-and-selected.json"))
    s04.write_text("{}")
    with pytest.raises(GateFailed, match="V07"):
        _runner(evidence, ReportUser(), object(), RecoveryTelemetry()).run_v07(
            run_id="run-controller-uid-1",
            incident_id="incident-run-one",
            investigation_id="investigation-run-one",
            destination_revision="notification-revision-1",
            narrative=_narrative(),
        )


def test_v07_rejects_s04_attestation_not_bound_to_receipt_hash(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path, "V07", s04_note_override="generic receipt confirmation")
    with pytest.raises(GateFailed, match="V07"):
        _runner(evidence, ReportUser(), object(), RecoveryTelemetry()).run_v07(
            run_id="run-controller-uid-1",
            incident_id="incident-run-one",
            investigation_id="investigation-run-one",
            destination_revision="notification-revision-1",
            narrative=_narrative(),
        )


def test_v07_rejects_s04_receipt_from_another_delivery(tmp_path: Path) -> None:
    evidence = _ledger(
        tmp_path,
        "V07",
        s04_receipt_overrides={"delivery_id": "another-delivery"},
    )
    with pytest.raises(GateFailed, match="V07"):
        _runner(evidence, ReportUser(), object(), RecoveryTelemetry()).run_v07(
            run_id="run-controller-uid-1",
            incident_id="incident-run-one",
            investigation_id="investigation-run-one",
            destination_revision="notification-revision-1",
            narrative=_narrative(),
        )


def test_v07_interruption_reconciles_publication_without_replay(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path, "V07")
    user = ReportUser()
    console = ReportConsole(user)
    paused = _runner(evidence, user, console, RecoveryTelemetry()).run_v07(
        run_id="run-controller-uid-1",
        incident_id="incident-run-one",
        investigation_id="investigation-run-one",
        destination_revision="notification-revision-1",
        narrative=_narrative(),
    )
    _attest(
        evidence, "V07", "sre",
        f"report_review_sha256={paused['report_review_sha256']}",
    )
    interrupted = InterruptedReportConsole(user, evidence)
    with pytest.raises(KeyboardInterrupt):
        _runner(evidence, user, interrupted, RecoveryTelemetry()).resume_v07(
            notification_admin=NotificationAdmin(),
            sre_username="pilot-sre",
            sre_password="password",
            attempts=1,
        )
    reopened = open_evidence(evidence.root)
    unused = ReportConsole(user)
    result = _runner(reopened, user, unused, RecoveryTelemetry()).resume_v07(
        notification_admin=NotificationAdmin(), attempts=1,
    )
    assert result["report_publication_id"] == "report-publication-1"
    assert unused.actions == []
