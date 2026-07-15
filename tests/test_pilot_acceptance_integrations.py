from __future__ import annotations

import json
from pathlib import Path

from aiops.acceptance.command import CommandResult
from aiops.acceptance.evidence import A01_GATE_SEQUENCE, AcceptanceEvidence
from aiops.acceptance.http import HttpResponse
from aiops.acceptance.connector_gate import ConnectorGateRunner
from aiops.acceptance.model_gate import ModelGateRunner, ModelInputs
from aiops.acceptance.notification_gate import (
    INVALID_NOTIFICATION_CONFIGS,
    NotificationGateRunner,
    NotificationInputs,
)
from aiops.acceptance.observability_gate import ObservabilityGateRunner
from notification_service.configuration import NotificationConfiguration
from notification_service.noise_controls import NotificationNoiseControls


ADMIN_PASSWORD = "admin-password-secret"
MODEL_KEY = "real-model-key-secret"
WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/real-webhook-secret"
CONNECTOR_CREDENTIAL = "connector-one-time-secret"


def _evidence(tmp_path: Path) -> AcceptanceEvidence:
    return AcceptanceEvidence.create(
        tmp_path / "acceptance",
        acceptance_id="v0.1.0-integrations",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        kube_context="clean",
        cluster_identity_sha256="b" * 64,
        access_profile="http_nodeport",
        now=lambda: "2026-07-14T01:02:03Z",
        attestation_verifier=lambda _item: None,
    )


class IntegrationSession:
    def __init__(self) -> None:
        self.model_revision = None
        self.model_state = "unverified"
        self.model_reason = None
        self.destination_revision = None
        self.destination_id = "destination-1"
        self.destination_enabled = False
        self.delivery = None
        self.pilot_route = False
        self.enrolled = False

    def request(self, method, path, *, body=None, csrf=True, request_id=None):
        if path == "/auth/reauth":
            assert body["password"] == ADMIN_PASSWORD
            return HttpResponse(200, {"request_id": request_id, "status": "ok"}, {})
        if path == "/api/v1/admin/audit":
            request_ids = (
                "acceptance-s03-invalid-save",
                "acceptance-s03-invalid-test",
                "acceptance-s03-real-save",
                "acceptance-s03-real-test",
            )
            return HttpResponse(
                200,
                {
                    "request_id": "audit-list",
                    "audit": [
                        {"request_id": item, "result": "success", "target_type": "model_provider"}
                        for item in request_ids
                    ],
                },
                {},
            )
        if path == "/api/v1/admin/model-provider" and method == "GET":
            return HttpResponse(200, {"request_id": "get-model", "model_provider": {"configuration_revision": self.model_revision}}, {})
        if path == "/api/v1/admin/model-provider" and method == "PUT":
            self.model_revision = "model:invalid" if body["api_key"].startswith("acceptance-invalid") else "model:real"
            self.model_state = "unverified"
            return HttpResponse(200, {"request_id": request_id, "model_provider": {"configuration_revision": self.model_revision}}, {})
        if path == "/api/v1/admin/model-provider/test":
            self.model_state = "failed" if self.model_revision == "model:invalid" else "verified"
            self.model_reason = "authentication_failed" if self.model_state == "failed" else None
            return HttpResponse(202, {"request_id": request_id, "verification": {"operation_id": f"op-{self.model_revision}", "revision": self.model_revision, "state": "verifying"}}, {})
        if path == "/api/v1/model-provider/status":
            return HttpResponse(200, {"request_id": "model-status", "model": {"readiness": "ready" if self.model_state == "verified" else "not_ready", "configuration_revision": self.model_revision, "verification": {"operation_id": "op", "state": self.model_state, "revision": self.model_revision, "checked_at": 1_700_000_000.0, "reason_code": self.model_reason}, "availability": {"state": "available", "observed_at": 1_700_000_000.0, "reason_code": self.model_reason}}}, {})
        if path == "/api/v1/admin/notification-destinations" and method == "GET":
            return HttpResponse(200, {"destinations": []}, {})
        if path == "/api/v1/admin/notification-destinations" and method == "POST":
            self.destination_revision = "notification:invalid"
            return HttpResponse(201, {"request_id": request_id, "destination": self._destination()}, {})
        if path == f"/api/v1/admin/notification-destinations/{self.destination_id}" and method == "PATCH":
            if body.get("expected_revision") != self.destination_revision:
                return HttpResponse(409, {"error": {"code": "notification_revision_conflict"}}, {})
            if "config" in body:
                if body.get("enabled"):
                    return HttpResponse(400, {"error": {"code": "notification_configuration_rejected"}}, {})
                self.destination_revision = "notification:real"
            else:
                assert body["enabled"] is True
                assert self.delivery == "delivery-sent"
                self.destination_enabled = True
            return HttpResponse(200, {"request_id": request_id, "destination": self._destination()}, {})
        if path == f"/api/v1/admin/notification-destinations/{self.destination_id}/test":
            if body.get("expected_revision") != self.destination_revision:
                return HttpResponse(409, {"error": {"code": "notification_revision_conflict"}}, {})
            self.delivery = "delivery-dead" if self.destination_revision == "notification:invalid" else "delivery-sent"
            return HttpResponse(202, {"request_id": request_id, "verification": {"operation_id": "notification-op", "delivery_id": self.delivery, "revision": self.destination_revision, "state": "verifying"}}, {})
        if path == "/api/v1/admin/notification-deliveries":
            status = "dead_letter" if self.delivery == "delivery-dead" else "sent"
            count = 3 if status == "dead_letter" else 1
            return HttpResponse(200, {"deliveries": [{"id": self.delivery, "status": status, "attempt_count": count, "attempts": [{"id": f"{self.delivery}:0:{number}", "attempt": number} for number in range(1, count + 1)], "last_reason_code": "connection_failed" if status == "dead_letter" else None}]}, {})
        if path == f"/api/v1/admin/notification-destinations/{self.destination_id}/select-pilot-route":
            if body.get("expected_revision") != self.destination_revision:
                return HttpResponse(409, {"error": {"code": "notification_revision_conflict"}}, {})
            assert self.destination_enabled is True
            self.pilot_route = True
            return HttpResponse(200, {"destination": self._destination()}, {})
        if path == "/api/v1/admin/notification-routes":
            return HttpResponse(200, {"routes": [{"id": "route:pilot-catch-all", "selected_destination_revision": self.destination_revision}]}, {})
        if path == "/api/v1/platform/status":
            return HttpResponse(200, {"capabilities": {
                "model": {"readiness": "ready", "configuration_revision": self.model_revision, "verification": {"revision": self.model_revision, "checked_at": 1_700_000_000.0}},
                "notification": {"readiness": "ready", "configuration_revision": self.destination_revision, "setup_decision": "active"},
                "connector": {"readiness": "ready", "configuration": "present", "verification": {"state": "verified"}, "connection": {"online": 1}},
            }}, {})
        if path == "/api/v1/admin/connector-enrollments" and method == "GET":
            if not self.enrolled:
                return HttpResponse(200, {"connector_enrollments": [], "clusters": []}, {})
            return HttpResponse(200, {"connector_enrollments": [{"id": "enrollment-1", "connector_id": "connector-dev", "cluster_id": "pilot-cluster", "active": True, "registered": True, "state": "online", "read_verification": "verified"}], "clusters": [{"connector_id": "connector-dev", "cluster_id": "pilot-cluster", "runtime_status": "online", "read_verification": {"status": "verified", "cluster_identity": {"connector_id": "connector-dev", "cluster_id": "pilot-cluster"}, "discovery": {"api_version": "v1", "kind": "Pod"}, "permission_summary": [{"verb": "get", "resource": "pods", "namespace": "aiops-system", "allowed": True}]}}]}, {})
        if path == "/api/v1/admin/connector-enrollments" and method == "POST":
            self.enrolled = True
            assert body["connector_id"] == "connector-dev"
            assert body["expected_revision"] is None
            return HttpResponse(201, {"request_id": request_id, "connector_enrollment": {"id": "enrollment-1", "connector_id": "connector-dev", "cluster_id": "pilot-cluster"}, "credential": CONNECTOR_CREDENTIAL}, {})
        raise AssertionError((method, path, body, csrf, request_id))

    def _destination(self):
        return {"id": self.destination_id, "configuration_revision": self.destination_revision, "enabled": self.destination_enabled, "pilot_route_selected": self.pilot_route, "readiness": "ready" if self.destination_revision == "notification:real" else "not_ready"}


class Commands:
    def __init__(self) -> None:
        self.secret_stdin = None

    def run(self, command, *, stdin=None, **_kwargs):
        command = tuple(command)
        if command[:3] == ("kubectl", "apply", "-f"):
            self.secret_stdin = stdin
        return CommandResult(command, 0, "ok", "", 0.1)


class Telemetry:
    def probe(self):
        return {
            "targets": {
                name: "up"
                for name in (
                    "prometheus", "alertmanager", "kube-state-metrics", "aiops-gateway",
                    "aiops-connector", "aiops-diagnosis", "aiops-notification",
                    "aiops-mcp-prometheus", "aiops-mcp-loki", "aiops-mcp-topology",
                )
            },
            "rules_loaded": True,
            "workload_series": 12,
            "loki_streams": 3,
            "alertmanager_route": True,
            "mcp_prometheus": {"status": "succeeded", "returned_series": 1},
            "mcp_loki": {"status": "succeeded", "returned_lines": 2},
        }


def _attest(evidence: AcceptanceEvidence, gate: str, role: str) -> None:
    statement = evidence.attestation_statement(
        actor="operator@example.test",
        role=role,
        gate_ids=[gate],
        conclusion="passed",
        note="observed one-time credential or message receipt",
    )
    evidence.append_attestation(statement, signature="sig", public_key="ssh-ed25519 AAAATEST", fingerprint="SHA256:test")


def _runner(tmp_path: Path):
    evidence = _evidence(tmp_path)
    session = IntegrationSession()
    commands = Commands()
    runners = {
        "model": ModelGateRunner(
            evidence=evidence,
            admin=session,
            sleep=lambda _seconds: None,
            now=lambda: 1_700_000_000.0,
        ),
        "notification": NotificationGateRunner(
            evidence=evidence, admin=session, sleep=lambda _seconds: None,
        ),
        "connector": ConnectorGateRunner(
            evidence=evidence, admin=session, commands=commands, sleep=lambda _seconds: None,
        ),
        "observability": ObservabilityGateRunner(evidence=evidence, telemetry=Telemetry()),
    }
    return evidence, session, commands, runners


def _advance(evidence: AcceptanceEvidence, gate_id: str) -> None:
    for predecessor in A01_GATE_SEQUENCE[: A01_GATE_SEQUENCE.index(gate_id)]:
        evidence.record_gate(
            predecessor,
            "not_applicable" if predecessor == "I04" else "passed",
            [],
        )


def test_s03_invalid_then_real_model_revision_is_verified_without_secret_evidence(tmp_path: Path) -> None:
    evidence, _session, _commands, runners = _runner(tmp_path)
    _advance(evidence, "S03")
    runners["model"].run_s03(
        ModelInputs("https://model.example.test/v1", "external", "model-1", 30, MODEL_KEY),
        admin_password=ADMIN_PASSWORD,
    )
    assert json.loads(evidence.manifest_path.read_text())["gates"]["S03"][0]["status"] == "passed"
    verified = json.loads(
        (evidence.root / "02-setup/S03-attempt-1/real-verification.json").read_text()
    )
    assert verified["platform_status"]["configuration_revision"] == "model:real"
    assert verified["fresh_until"] == 1_700_000_900.0
    assert MODEL_KEY not in "\n".join(path.read_text(errors="ignore") for path in evidence.root.rglob("*") if path.is_file())


def test_s04_dead_letter_then_sent_selected_route_requires_receipt(tmp_path: Path) -> None:
    evidence, session, _commands, runners = _runner(tmp_path)
    _advance(evidence, "S04")
    runners["notification"].run_s04(
        NotificationInputs("feishu", {"webhook_url": WEBHOOK}),
        admin_password=ADMIN_PASSWORD,
        confirm_receipt=lambda _delivery: _attest(evidence, "S04", "platform_administrator"),
    )
    assert session.pilot_route is True
    selected = json.loads(
        (evidence.root / "02-setup/S04-attempt-1/sent-and-selected.json").read_text()
    )
    assert selected["attempt_ids"] == ["delivery-sent:0:1"]
    assert selected["route_revision"] == "notification:real"
    persisted = "\n".join(path.read_text(errors="ignore") for path in evidence.root.rglob("*") if path.is_file())
    assert WEBHOOK not in persisted


def test_s04_invalid_configs_reach_the_notification_delivery_boundary(tmp_path: Path) -> None:
    key = tmp_path / "notification.key"
    key.write_bytes(b"k" * 32)
    database = tmp_path / "notification.db"
    owner = NotificationConfiguration(
        database,
        key,
        NotificationNoiseControls(database),
    )

    for provider, config in INVALID_NOTIFICATION_CONFIGS.items():
        created = owner.create_destination({
            "name": f"A01 invalid {provider}",
            "provider": provider,
            "config": config,
        })
        assert created["provider"] == provider


def test_s05_enrollment_credential_goes_only_to_kubernetes_secret_and_read_verifies(tmp_path: Path) -> None:
    evidence, _session, commands, runners = _runner(tmp_path)
    _advance(evidence, "S05")
    runners["connector"].run_s05(
        admin_password=ADMIN_PASSWORD,
        connector_id="connector-dev",
        cluster_id="pilot-cluster",
        confirm_one_time=lambda: _attest(evidence, "S05", "platform_operator"),
    )
    assert CONNECTOR_CREDENTIAL in commands.secret_stdin
    verification = json.loads(
        (evidence.root / "02-setup/S05-attempt-1/connector-read-verification.json").read_text()
    )
    assert verification["platform_status"]["readiness"] == "ready"
    persisted = "\n".join(path.read_text(errors="ignore") for path in evidence.root.rglob("*") if path.is_file())
    assert CONNECTOR_CREDENTIAL not in persisted


def test_s06_accepts_only_real_owner_and_guarded_mcp_summary(tmp_path: Path) -> None:
    evidence, _session, _commands, runners = _runner(tmp_path)
    _advance(evidence, "S06")
    runners["observability"].run_s06()
    assert json.loads(evidence.manifest_path.read_text())["gates"]["S06"][0]["status"] == "passed"
