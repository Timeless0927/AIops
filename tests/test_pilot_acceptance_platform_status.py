from __future__ import annotations

import json
from pathlib import Path

from aiops.acceptance.evidence import A01_GATE_SEQUENCE, AcceptanceEvidence
from tests.pilot_acceptance_support import create_evidence
from aiops.acceptance.http import HttpResponse
from aiops.acceptance.platform_status_gates import PlatformStatusGateRunner
from aiops.acceptance.web_gates import BrowserResult


PASSWORD = "admin-secret"


def _evidence(tmp_path: Path) -> AcceptanceEvidence:
    return create_evidence(
        tmp_path / "acceptance",
        acceptance_id="v0.1.0-setup-status",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        acceptance_tool_sha256="c" * 64,
        gate_contract_revision="pilot-clean-acceptance-v5",
        kube_context="clean",
        cluster_identity_sha256="b" * 64,
        access_profile="http_nodeport",
        now=lambda: "2026-07-14T01:02:03Z",
    )


def _capability(readiness="not_ready", revision=None, decision="active"):
    return {
        "readiness": readiness,
        "configuration": "absent" if revision is None else "present",
        "configuration_revision": revision,
        "setup_decision": decision,
        "verification": {
            "operation_id": None,
            "state": "unverified",
            "revision": revision,
            "checked_at": None,
            "reason_code": None,
        },
        "availability": {"state": "available", "observed_at": 1, "reason_code": None},
    }


class State:
    notification_skipped = False

    def __init__(self):
        self.notification_skipped = False
        self.audit = []

    def platform(self):
        notification = _capability(
            "skipped" if self.notification_skipped else "not_ready",
            None,
            "skipped" if self.notification_skipped else "active",
        )
        return {
            "request_id": "request-1",
            "generated_at": 1,
            "capabilities": {
                "model": _capability(),
                "notification": notification,
                "connector": _capability(),
                "observability": _capability("ready", "observability:1"),
            },
        }


class Session:
    def __init__(self, state: State, admin: bool, stale: bool = False) -> None:
        self.state = state
        self.admin = admin
        self.stale = stale

    def request(self, method, path, *, body=None, csrf=True, request_id=None):
        if path == "/api/v1/platform/status":
            return HttpResponse(200, self.state.platform(), {})
        if path == "/api/v1/incidents":
            return HttpResponse(200, {"incidents": []}, {})
        if path == "/auth/reauth":
            assert body["password"] == PASSWORD
            return HttpResponse(200, {"status": "ok"}, {})
        if path == "/api/v1/admin/audit":
            return HttpResponse(200, {"request_id": "audit-list", "audit": self.state.audit}, {})
        if path.endswith("/setup-decision"):
            if not self.admin:
                return HttpResponse(403, {"error": {"code": "forbidden"}}, {})
            if not csrf:
                self.state.audit.append({"request_id": request_id, "result": "csrf_required"})
                return HttpResponse(403, {"error": {"code": "csrf_required"}}, {})
            if "reason" not in body or "expected_revision" not in body:
                return HttpResponse(400, {"error": {"code": "invalid_request"}}, {})
            if self.stale:
                self.state.audit.append({"request_id": request_id, "result": "fresh_auth_required"})
                return HttpResponse(403, {"error": {"code": "fresh_auth_required"}}, {})
            self.state.notification_skipped = body["setup_decision"] == "skipped"
            self.state.audit.append({"request_id": request_id, "result": "success"})
            return HttpResponse(
                200,
                {
                    "request_id": request_id,
                    "setup_decision": body
                    | {"actor_id": "admin", "decided_at": 1},
                },
                {},
            )
        if path in {
            "/api/v1/admin/model-provider",
            "/api/v1/admin/notification-destinations",
            "/api/v1/admin/connector-enrollments",
        } and method == "GET":
            return HttpResponse(200 if self.admin else 403, {}, {})
        if path in {
            "/api/v1/admin/model-provider/test",
            "/api/v1/admin/notification-destinations/acceptance-missing/test",
        } or (path, method) in {
            ("/api/v1/admin/model-provider", "PUT"),
            ("/api/v1/admin/notification-destinations", "POST"),
            ("/api/v1/admin/connector-enrollments", "POST"),
        }:
            if not self.admin:
                return HttpResponse(403, {"error": {"code": "forbidden"}}, {})
            if not csrf:
                self.state.audit.append({"request_id": request_id, "result": "csrf_required"})
                return HttpResponse(403, {"error": {"code": "csrf_required"}}, {})
            required = {"reason"}
            if "test" in path:
                required.add("expected_revision")
            if (path, method) == ("/api/v1/admin/model-provider", "PUT"):
                required.update({
                    "endpoint", "endpoint_scope", "model", "timeout_seconds", "api_key",
                    "expected_revision",
                })
            if (path, method) == ("/api/v1/admin/connector-enrollments", "POST"):
                required.add("expected_revision")
            if not required <= set(body):
                return HttpResponse(400, {"error": {"code": "invalid_request"}}, {})
            if self.stale:
                self.state.audit.append({"request_id": request_id, "result": "fresh_auth_required"})
                return HttpResponse(403, {"error": {"code": "fresh_auth_required"}}, {})
            raise AssertionError("guarded probe would mutate with fresh auth")
        raise AssertionError((method, path, body, csrf, request_id))


class Browser:
    def probe(self, base_url, *, username=None, password=None):
        assert password == PASSWORD
        return BrowserResult(
            summary={
                "same_origin": True,
                "origins": [base_url],
                "paths": ["/", "/assets/index.js", "/auth/login", "/api/v1/actor", "/platform"],
                "desktop_nav_reentry": True,
                "mobile_no_overflow": True,
            },
            screenshots={"platform-desktop.png": b"png", "platform-mobile.png": b"png"},
        )


def _advance(evidence: AcceptanceEvidence, gate_id: str) -> None:
    for predecessor in A01_GATE_SEQUENCE[: A01_GATE_SEQUENCE.index(gate_id)]:
        evidence.start_gate(predecessor)
        evidence.record_gate(
            predecessor,
            "not_applicable" if predecessor == "I04" else "passed",
            [],
        )


def test_s01_skip_persists_across_relogin_and_incident_workspace_stays_open(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    _advance(evidence, "S01")
    state = State()
    runner = PlatformStatusGateRunner(
        evidence=evidence,
        admin=Session(state, True),
        relogin=lambda: Session(state, True),
        stale_admin=lambda: Session(state, True, stale=True),
        user=Session(state, False),
        browser=Browser(),
        base_url="http://192.0.2.10:30088",
        sleep=lambda _seconds: None,
    )

    runner.run_s01(admin_username="admin", admin_password=PASSWORD)

    manifest = json.loads(evidence.manifest_path.read_text())
    assert manifest["gates"]["S01"][0]["status"] == "passed"
    assert state.notification_skipped is True
    setup_audit = json.loads(
        (evidence.root / "02-setup/S01-attempt-1/setup-audit.json").read_text()
    )
    assert setup_audit == [
        {"request_id": "acceptance-s01-notification-skip", "result": "success"}
    ]


def test_s02_proves_safe_user_projection_and_admin_mutation_guards(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    _advance(evidence, "S02")
    state = State()
    state.notification_skipped = True
    runner = PlatformStatusGateRunner(
        evidence=evidence,
        admin=Session(state, True),
        relogin=lambda: Session(state, True),
        stale_admin=lambda: Session(state, True, stale=True),
        user=Session(state, False),
        browser=Browser(),
        base_url="http://192.0.2.10:30088",
        sleep=lambda _seconds: None,
    )

    runner.run_s02(admin_password=PASSWORD)

    manifest = json.loads(evidence.manifest_path.read_text())
    assert manifest["gates"]["S02"][0]["status"] == "passed"
    role_matrix = json.loads(
        (evidence.root / "02-setup/S02-attempt-1/role-negative-matrix.json").read_text()
    )
    assert role_matrix["model-configure"] == 403
    assert role_matrix["notification-configure"] == 403
    persisted = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in evidence.root.rglob("*")
        if path.is_file()
    )
    assert PASSWORD not in persisted
