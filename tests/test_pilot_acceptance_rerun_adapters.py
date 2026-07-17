from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiops.acceptance.command import CommandResult
from aiops.acceptance.rerun import RerunScope
from aiops.acceptance.http import HttpResponse
from aiops.acceptance.rerun_adapters import GatewayRerunChainAdapter, KubectlRerunAdapter
from aiops.acceptance.web_gates import BrowserResult


class Commands:
    def __init__(self, results: list[CommandResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, ...]] = []

    def run(self, command, **_kwargs):
        self.calls.append(tuple(command))
        return self.results.pop(0)


def _result(command: str, stdout: str = "") -> CommandResult:
    return CommandResult(tuple(command.split()), 0, stdout, "", 1)


def _job(run_id: str) -> str:
    return json.dumps({
        "metadata": {
            "name": "verification-trigger", "namespace": "aiops-verification",
            "uid": run_id,
        },
        "status": {"startTime": "2026-07-17T01:02:03Z", "succeeded": 1},
    })


def test_v08_adapter_creates_and_reconciles_one_new_terminal_job(tmp_path: Path) -> None:
    run_id = "run-controller-uid-2"
    log = json.dumps({"event": "verification_trigger_job_succeeded", "run_id": run_id})
    commands = Commands([
        _result("delete"), _result("create"), _result("wait"),
        _result("get", _job(run_id)), _result("logs", log),
        _result("get", _job(run_id)), _result("logs", log),
    ])
    adapter = KubectlRerunAdapter(commands, kube_context="pilot-context")
    scope = RerunScope(
        release_root=tmp_path, old_run_id="run-controller-uid-1",
        incident_id="incident-run-one",
    )

    created = adapter.trigger(scope, operation_id="v08/trigger")
    reconciled = adapter.reconcile_trigger(scope, operation_id="v08/trigger")

    assert created == reconciled
    assert created["run_id"] == run_id
    assert created["trigger_started_at"] == 1784250123.0
    assert commands.calls[:3] == [
        (
            "kubectl", "--context", "pilot-context", "delete", "job",
            "verification-trigger", "-n", "aiops-verification",
            "--ignore-not-found", "--wait=true",
        ),
        (
            "kubectl", "--context", "pilot-context", "create", "-k",
            str(tmp_path / "verification/run"),
        ),
        (
            "kubectl", "--context", "pilot-context", "wait", "-n",
            "aiops-verification", "--for=condition=complete",
            "job/verification-trigger", "--timeout=2m",
        ),
    ]


def test_v08_gateway_adapter_preserves_v1_and_selects_second_resolved_delivery(
    tmp_path: Path,
) -> None:
    report_v1 = {
        "id": "report-v1", "version": 1, "status": "published",
        "included_investigation_ids": ["investigation-v1"],
    }
    report_v2 = {
        "id": "report-v2", "version": 2, "status": "published",
        "included_investigation_ids": ["investigation-v1", "investigation-v2"],
        "narrative": {"impact": "impact"},
    }
    delivery = {
        "id": "delivery-v2", "event_id": "incident.resolved:incident-1:2",
        "is_test": False, "status": "sent", "request_id": "request-v2",
        "provider_identity": "provider-v2", "destination_id": "destination-1",
        "destination_revision": "7",
    }

    class Console:
        def publish_v08(self, **_kwargs):
            paths = [
                "/api/v1/incidents/incident-1/report",
                "/api/v1/incidents/incident-1/report/publish",
            ]
            return BrowserResult({
                "same_origin": True, "origins": ["https://aiops.example"],
                "screenshots_masked": True,
                "mutations": [{"path": path} for path in paths],
            }, {"v08-report.png": b"masked"})

    class User:
        def request(self, method: str, path: str):
            assert method == "GET" and path.endswith("/report")
            return HttpResponse(200, {"publications": [report_v2, report_v1]}, {})

    class Admin:
        def request(self, method: str, path: str):
            assert method == "GET" and path == "/api/v1/admin/notification-deliveries"
            return HttpResponse(200, {"deliveries": [
                {**delivery, "id": "delivery-v1", "event_id": "incident.resolved:incident-1:1"},
                delivery,
            ]}, {})

    adapter = GatewayRerunChainAdapter(
        console=Console(), user=User(), notification_admin=Admin(), telemetry=object(),
        base_url="https://aiops.example", sleep=lambda _seconds: None, attempts=1,
    )
    scope = RerunScope(
        release_root=tmp_path, old_run_id="run-1", incident_id="incident-1",
        old_investigation_id="investigation-v1", old_delivery_id="delivery-v1",
        report_v1=report_v1,
        destination={"id": "destination-1", "revision": "7"},
    )
    executed = {"run_id": "run-2", "investigation_id": "investigation-v2"}

    result = adapter.publish(
        scope, executed, {"impact": "impact"}, operation_id="v08/publish",
        sre_username="sre", sre_password="in-memory",
    )

    assert result["report_v1"] == report_v1
    assert result["report_v2"] == report_v2
    assert result["notification_delivery"] == delivery


def _browser(action: str, paths: list[str], **summary) -> BrowserResult:
    return BrowserResult({
        "action": action, "same_origin": True,
        "origins": ["https://aiops.example"], "screenshots_masked": True,
        "mutations": [{"path": path} for path in paths], **summary,
    }, {})


class ReceiptConsole:
    def __init__(self) -> None:
        self.receipt_credentials: tuple[str, str] | None = None

    def reinvestigate_v08(self, **_kwargs):
        return _browser(
            "v08_reinvestigate", ["/api/v1/incidents/incident-1/reinvestigate"],
            investigation={"id": "investigation-v2", "sequence": 2},
        )

    def create_v08(self, **_kwargs):
        return _browser(
            "v08_create", ["/api/v1/incidents/incident-1/change-requests"],
            change_request={"id": "change-v2"},
        )

    def verify_v08_denial(self, **_kwargs):
        root = "/api/v1/change-requests/change-v2"
        return _browser(
            "v08_denial",
            [f"{root}/phase-approval/approve", f"{root}/phase-execution/start"],
        )

    def verify_v08_destination(self, **kwargs):
        self.receipt_credentials = (kwargs["username"], kwargs["password"])
        path = "/api/v1/admin/notification-destinations/destination-1/test"
        return _browser(
            "v08_destination_receipt", [path],
            verification={"delivery_id": "delivery-test-8", "revision": "8"},
        )


class ReceiptOnlyAdapter(GatewayRerunChainAdapter):
    def _public_signal(self, _scope, _trigger):
        return {
            "investigation": {"id": "investigation-v1"},
            "alert_signal": {"fingerprint": "fingerprint-v2"},
        }

    def _diagnosis(self, _scope, _investigation_id, _run_id, _fingerprint):
        return {
            "recommended_action": {"id": "action-v2", "summary": "repair"},
            "evidence_steps": [{"id": "evidence-v2"}],
        }

    def _change_review(self, _change_request_id):
        return (
            {"id": "change-v2", "status": "awaiting_approval"},
            {
                "phase_id": "phase-v2", "revision_id": "revision-v2",
                "changes": [{"dry_run_hash": "e" * 64, "target_confirmation": "target-v2"}],
            },
        )

    @staticmethod
    def _denial(_result, _change_request_id):
        return None

    def _report_v1(self, scope):
        return dict(scope.report_v1 or {})


class ReceiptAdmin:
    def __init__(self, status: str) -> None:
        self.status = status

    def request(self, method: str, path: str):
        assert method == "GET"
        if path == "/api/v1/admin/notification-destinations":
            return HttpResponse(200, {"destinations": [{
                "id": "destination-1", "name": "Pilot Destination",
                "configuration_revision": "8",
            }]}, {})
        assert path == "/api/v1/admin/notification-deliveries"
        return HttpResponse(200, {"deliveries": [{
            "id": "delivery-test-8", "status": self.status, "is_test": True,
            "destination_id": "destination-1", "destination_revision": "8",
            "attempt_count": 1, "attempts": [{"id": "attempt-test-8"}],
            "provider_identity": "provider-test-8",
        }]}, {})


def _prepare_with_changed_destination(tmp_path: Path, status: str):
    console = ReceiptConsole()
    adapter = ReceiptOnlyAdapter(
        console=console, user=object(), notification_admin=ReceiptAdmin(status),
        telemetry=object(), base_url="https://aiops.example",
        sleep=lambda _seconds: None, attempts=1,
    )
    scope = RerunScope(
        release_root=tmp_path, old_run_id="run-1", incident_id="incident-1",
        old_investigation_id="investigation-v1", report_v1={"id": "report-v1"},
        destination={"id": "destination-1", "revision": "7"},
    )
    result = adapter.prepare(
        scope, {"run_id": "run-2"}, operation_id="v08/prepare",
        sre_username="sre", sre_password="sre-password",
        no_authority_username="ordinary", no_authority_password="ordinary-password",
        platform_admin_username="platform-admin", platform_admin_password="admin-password",
    )
    return result, console


def test_v08_gateway_adapter_reverifies_changed_destination_revision(tmp_path: Path) -> None:
    result, console = _prepare_with_changed_destination(tmp_path, "sent")

    assert result["destination"] == {"id": "destination-1", "revision": "8"}
    assert result["receipt_review"] == {
        "destination_id": "destination-1", "revision": "8",
        "delivery_id": "delivery-test-8", "status": "sent",
        "attempt_count": 1, "attempt_ids": ["attempt-test-8"],
        "provider_identity": "provider-test-8",
    }
    assert console.receipt_credentials == ("platform-admin", "admin-password")


@pytest.mark.parametrize("status", ["failed", "dead_letter"])
def test_v08_gateway_adapter_blocks_failed_changed_destination_receipt(
    tmp_path: Path, status: str,
) -> None:
    with pytest.raises(ValueError, match="receipt Delivery failed"):
        _prepare_with_changed_destination(tmp_path, status)
