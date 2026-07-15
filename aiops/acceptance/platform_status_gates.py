"""A01 Platform Status and role-safety gates."""

from __future__ import annotations

import json
import time
from typing import Callable

from .evidence import AcceptanceEvidence, Artifact
from .http import GatewaySession
from .integration_support import fail_gate
from .web_gates import BrowserProbe, assert_same_origin_browser


DECISION_PATH = "/api/v1/admin/platform/capabilities/notification/setup-decision"


class PlatformStatusGateRunner:
    def __init__(
        self,
        *,
        evidence: AcceptanceEvidence,
        admin: GatewaySession,
        relogin: Callable[[], GatewaySession],
        stale_admin: Callable[[], GatewaySession],
        user: GatewaySession,
        browser: BrowserProbe,
        base_url: str,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.evidence = evidence
        self.admin = admin
        self.relogin = relogin
        self.stale_admin = stale_admin
        self.user = user
        self.browser = browser
        self.base_url = base_url
        self.sleep = sleep

    def run_s01(self, *, admin_username: str, admin_password: str) -> None:
        started_at = self.evidence.start_gate("S01")
        artifacts: list[Artifact] = []
        try:
            initial = self._platform(self.admin)
            self._validate_platform(initial)
            notification = initial["capabilities"]["notification"]
            skipped = self.admin.request(
                "PUT",
                DECISION_PATH,
                body={
                    "setup_decision": "skipped",
                    "expected_revision": notification["configuration_revision"],
                    "reason": "A01 verify optional setup remains non-blocking",
                },
                request_id="acceptance-s01-notification-skip",
            )
            if skipped.status != 200:
                raise ValueError("Platform Administrator could not skip optional Notification")
            setup_audit_response = self.admin.request("GET", "/api/v1/admin/audit")
            setup_audit = [
                row
                for row in setup_audit_response.body.get("audit", [])
                if row.get("request_id") == "acceptance-s01-notification-skip"
            ] if setup_audit_response.status == 200 else []
            if len(setup_audit) != 1:
                raise ValueError("Notification setup decision is missing Gateway audit correlation")
            after_skip = self._platform(self.admin)
            skipped_capability = after_skip["capabilities"]["notification"]
            if (
                skipped_capability["readiness"] != "skipped"
                or skipped_capability["setup_decision"] != "skipped"
            ):
                raise ValueError("skipped Notification was reported ready or active")
            workspace = self.admin.request("GET", "/api/v1/incidents")
            if workspace.status != 200:
                raise ValueError("Incident workspace became inaccessible during optional setup")
            reopened = self._platform(self.relogin())
            if reopened["capabilities"]["notification"] != skipped_capability:
                raise ValueError("Notification skip did not persist across login")
            browser = self.browser.probe(
                self.base_url, username=admin_username, password=admin_password
            )
            assert_same_origin_browser(browser, self.base_url)
            if "/platform" not in browser.summary.get("paths", []):
                raise ValueError("browser did not re-enter Platform Status from normal navigation")
            if (
                browser.summary.get("desktop_nav_reentry") is not True
                or browser.summary.get("mobile_no_overflow") is not True
            ):
                raise ValueError("Platform Status navigation or 390px overflow check failed")
            artifacts.extend(
                [
                    self.evidence.write_json("S01", "platform-initial.json", initial),
                    self.evidence.write_json(
                        "S01", "setup-decision.json", skipped.body
                    ),
                    self.evidence.write_json("S01", "setup-audit.json", setup_audit),
                    self.evidence.write_json("S01", "platform-skipped.json", after_skip),
                    self.evidence.write_json("S01", "platform-relogin.json", reopened),
                    self.evidence.write_json(
                        "S01",
                        "browser-network.json",
                        self.evidence.contextualize(browser.summary),
                    ),
                ]
            )
            artifacts.extend(
                self.evidence.write_bytes("S01", name, content)
                for name, content in sorted(browser.screenshots.items())
            )
            self.evidence.record_gate("S01", "passed", artifacts, started_at=started_at)
        except Exception as exc:
            fail_gate(self.evidence, "S01", artifacts, exc, (admin_password,), started_at)

    def run_s02(self, *, admin_password: str) -> None:
        started_at = self.evidence.start_gate("S02")
        artifacts: list[Artifact] = []
        try:
            user_status = self._platform(self.user)
            serialized = json.dumps(user_status, sort_keys=True).lower()
            forbidden = ("endpoint", "recipient", "webhook", "secret", "credential", "api_key")
            if any(term in serialized for term in forbidden):
                raise ValueError("ordinary User Platform Status exposed integration metadata")
            forbidden_matrix: dict[str, int] = {}
            for path in self._admin_read_paths():
                response = self.user.request("GET", path)
                forbidden_matrix[path] = response.status
            notification = user_status["capabilities"]["notification"]
            user_mutation = self.user.request(
                "PUT",
                DECISION_PATH,
                body={
                    "setup_decision": "active",
                    "expected_revision": notification["configuration_revision"],
                    "reason": "ordinary User must not configure setup",
                },
                request_id="acceptance-s02-user-denied",
            )
            forbidden_matrix[DECISION_PATH] = user_mutation.status
            for name, method, path, body in self._guarded_mutations():
                forbidden_matrix[name] = self.user.request(
                    method,
                    path,
                    body=body,
                    request_id=f"acceptance-s02-user-{name}",
                ).status
            if set(forbidden_matrix.values()) != {403}:
                raise ValueError("ordinary User reached an administration surface")
            reauth = self.admin.request(
                "POST",
                "/auth/reauth",
                body={"password": admin_password},
                request_id="acceptance-s02-reauth",
            )
            csrf_matrix = {
                name: self.admin.request(
                    method,
                    path,
                    body=body,
                    csrf=False,
                    request_id=f"acceptance-s02-no-csrf-{name}",
                ).status
                for name, method, path, body in self._guarded_mutations()
            }
            no_csrf = self.admin.request(
                "PUT", DECISION_PATH,
                body={"setup_decision": "active", "expected_revision": notification["configuration_revision"], "reason": "missing CSRF must fail"},
                csrf=False, request_id="acceptance-s02-no-csrf-setup-decision",
            )
            csrf_matrix["setup-decision"] = no_csrf.status
            missing_reason = self.admin.request(
                "PUT",
                DECISION_PATH,
                body={
                    "setup_decision": "active",
                    "expected_revision": notification["configuration_revision"],
                },
                request_id="acceptance-s02-no-reason",
            )
            missing_revision = self.admin.request(
                "PUT",
                DECISION_PATH,
                body={"setup_decision": "active", "reason": "missing revision must fail"},
                request_id="acceptance-s02-no-revision",
            )
            field_matrix = {
                "setup_missing_reason": missing_reason.status,
                "setup_missing_revision": missing_revision.status,
                "model_missing_reason": self.admin.request(
                    "POST", "/api/v1/admin/model-provider/test",
                    body={"expected_revision": "acceptance-missing"},
                    request_id="acceptance-s02-model-no-reason",
                ).status,
                "model_missing_revision": self.admin.request(
                    "POST", "/api/v1/admin/model-provider/test",
                    body={"reason": "missing revision must fail"},
                    request_id="acceptance-s02-model-no-revision",
                ).status,
                "notification_missing_reason": self.admin.request(
                    "POST", "/api/v1/admin/notification-destinations/acceptance-missing/test",
                    body={"expected_revision": "acceptance-missing"},
                    request_id="acceptance-s02-notification-no-reason",
                ).status,
                "notification_missing_revision": self.admin.request(
                    "POST", "/api/v1/admin/notification-destinations/acceptance-missing/test",
                    body={"reason": "missing revision must fail"},
                    request_id="acceptance-s02-notification-no-revision",
                ).status,
                "connector_missing_reason": self.admin.request(
                    "POST", "/api/v1/admin/connector-enrollments",
                    body={
                        "connector_id": "acceptance-stale",
                        "cluster_id": "acceptance-stale",
                        "expected_revision": None,
                    },
                    request_id="acceptance-s02-connector-no-reason",
                ).status,
                "connector_missing_revision": self.admin.request(
                    "POST", "/api/v1/admin/connector-enrollments",
                    body={
                        "connector_id": "acceptance-stale",
                        "cluster_id": "acceptance-stale",
                        "reason": "missing revision must fail",
                    },
                    request_id="acceptance-s02-connector-no-revision",
                ).status,
            }
            stale = self.stale_admin()
            self.sleep(301)
            stale_matrix = {
                name: stale.request(
                    method,
                    path,
                    body=body,
                    request_id=f"acceptance-s02-stale-{name}",
                ).status
                for name, method, path, body in self._guarded_mutations()
            }
            stale_matrix["setup-decision"] = stale.request(
                "PUT", DECISION_PATH,
                body={"setup_decision": "active", "expected_revision": notification["configuration_revision"], "reason": "stale auth must fail"},
                request_id="acceptance-s02-stale-setup-decision",
            ).status
            expected_audit_ids = {
                *(f"acceptance-s02-no-csrf-{name}" for name in csrf_matrix),
                *(f"acceptance-s02-stale-{name}" for name in stale_matrix),
            }
            audit_response = self.admin.request("GET", "/api/v1/admin/audit")
            audit_rows = [
                row
                for row in audit_response.body.get("audit", [])
                if row.get("request_id") in expected_audit_ids
            ] if audit_response.status == 200 else []
            if {row.get("request_id") for row in audit_rows} != expected_audit_ids:
                raise ValueError("mutation guard attempts are missing admin audit correlation")
            guard_matrix = {
                "fresh_reauthentication": reauth.status,
                "csrf_matrix": csrf_matrix,
                "required_field_matrix": field_matrix,
                "stale_auth_matrix": stale_matrix,
                "explicit_request_ids": sorted(expected_audit_ids),
            }
            if (
                reauth.status != 200
                or set(csrf_matrix.values()) != {403}
                or set(field_matrix.values()) != {400}
                or set(stale_matrix.values()) != {403}
            ):
                raise ValueError("administrator mutation guard matrix failed")
            artifacts.extend(
                [
                    self.evidence.write_json("S02", "user-platform-status.json", user_status),
                    self.evidence.write_json("S02", "role-negative-matrix.json", forbidden_matrix),
                    self.evidence.write_json("S02", "mutation-guard-matrix.json", guard_matrix),
                    self.evidence.write_json("S02", "audit-correlation.json", audit_rows),
                ]
            )
            self.evidence.record_gate("S02", "passed", artifacts, started_at=started_at)
        except Exception as exc:
            fail_gate(self.evidence, "S02", artifacts, exc, (admin_password,), started_at)

    @staticmethod
    def _admin_read_paths() -> tuple[str, ...]:
        return (
            "/api/v1/admin/model-provider",
            "/api/v1/admin/notification-destinations",
            "/api/v1/admin/connector-enrollments",
        )

    @staticmethod
    def _guarded_mutations() -> tuple[tuple[str, str, str, dict], ...]:
        return (
            (
                "model-configure", "PUT", "/api/v1/admin/model-provider", {
                    "endpoint": "https://127.0.0.1/acceptance-not-sent",
                    "endpoint_scope": "external",
                    "model": "acceptance-not-sent",
                    "timeout_seconds": 5,
                    "api_key": "acceptance-placeholder",
                    "expected_revision": None,
                    "reason": "authorization guard probe",
                },
            ),
            (
                "model-test", "POST", "/api/v1/admin/model-provider/test",
                {"expected_revision": "acceptance-missing", "reason": "authorization guard probe"},
            ),
            (
                "notification-configure", "POST",
                "/api/v1/admin/notification-destinations", {
                    "name": "Acceptance not sent",
                    "provider": "feishu",
                    "config": {"webhook_url": "https://127.0.0.1/acceptance-not-sent"},
                    "reason": "authorization guard probe",
                },
            ),
            (
                "notification-test", "POST",
                "/api/v1/admin/notification-destinations/acceptance-missing/test",
                {"expected_revision": "acceptance-missing", "reason": "authorization guard probe"},
            ),
            (
                "connector-create", "POST", "/api/v1/admin/connector-enrollments",
                {
                    "connector_id": "acceptance-stale",
                    "cluster_id": "acceptance-stale",
                    "expected_revision": None,
                    "reason": "authorization guard probe",
                },
            ),
        )

    @staticmethod
    def _platform(session: GatewaySession) -> dict:
        response = session.request("GET", "/api/v1/platform/status")
        if response.status != 200 or not isinstance(response.body, dict):
            raise ValueError("Platform Status is unavailable")
        return response.body

    @staticmethod
    def _validate_platform(status: dict) -> None:
        capabilities = status.get("capabilities")
        if not isinstance(capabilities, dict) or set(capabilities) != {
            "model",
            "notification",
            "connector",
            "observability",
        }:
            raise ValueError("Platform Status does not contain four independent capabilities")
        serialized = json.dumps(status, sort_keys=True)
        if "all_ready" in serialized or "setup_complete" in serialized:
            raise ValueError("Platform Status exposed a forbidden aggregate setup state")
        for capability in ("model", "notification", "connector"):
            item = capabilities[capability]
            if item.get("configuration") != "absent" or item.get("readiness") != "not_ready":
                raise ValueError(f"unconfigured {capability} capability reported false readiness")
