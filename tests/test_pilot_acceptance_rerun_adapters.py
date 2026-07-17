from __future__ import annotations

import json
from pathlib import Path

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
