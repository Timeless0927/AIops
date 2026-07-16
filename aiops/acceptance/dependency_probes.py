"""Public product probes for R03/R04 dependency degradation."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Protocol

from .credentials import CredentialValue, assert_public_payload
from .recovery import RecoveryScope, RecoveryTarget
from .verification_trigger import UserSession
from .web_gates import BrowserResult


class DependencyBrowser(Protocol):
    def prepare_r03(
        self,
        *,
        base_url: str,
        username: str,
        password: str | CredentialValue,
        incident_id: str,
        connector_id: str,
        cluster_id: str,
        operation_id: str,
    ) -> BrowserResult: ...

    def verify_r03_admin_denial(
        self,
        *,
        base_url: str,
        username: str,
        password: str | CredentialValue,
        cluster_id: str,
        operation_id: str,
    ) -> BrowserResult: ...

    def verify_r03_sre_denials(
        self,
        *,
        base_url: str,
        username: str,
        password: str | CredentialValue,
        incident_id: str,
        prepared: dict[str, object],
        operation_id: str,
    ) -> BrowserResult: ...


class ConnectorPollProbe(Protocol):
    def unavailable(self, scope: RecoveryScope, *, request_id: str) -> dict[str, object]: ...


class LokiDependencyProbe(Protocol):
    def unavailable(self, scope: RecoveryScope, *, request_id: str) -> dict[str, object]: ...

    def recovered(self, scope: RecoveryScope, *, request_id: str) -> dict[str, object]: ...


class ConnectorPollGatewayProbe:
    """Calls the exact Connector poll endpoint with a run-scoped credential."""

    def __init__(
        self, base_url: str, credential: CredentialValue, *, timeout: float = 15,
    ) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Connector poll base URL must be absolute HTTP(S)")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Connector poll base URL cannot contain credentials")
        self.base_url = base_url.rstrip("/")
        self.credential = credential
        self.timeout = timeout
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )

    def unavailable(self, scope: RecoveryScope, *, request_id: str) -> dict[str, object]:
        body = json.dumps({
            "connector_id": scope.connector_id,
            "cluster_id": scope.cluster_id,
            "wait_seconds": 0,
        }, separators=(",", ":")).encode()
        request = urllib.request.Request(
            self.base_url + "/api/v1/connectors/commands/poll",
            data=body,
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer " + self.credential.reveal(),
                "Content-Type": "application/json",
                "X-Request-ID": request_id,
            },
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                status = response.status
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            status = exc.code
            payload = json.loads(exc.read())
        error = payload.get("error") if isinstance(payload, dict) else None
        if (
            status != 409
            or not isinstance(error, dict)
            or error.get("code") != "cluster_not_ready"
            or payload.get("request_id") != request_id
        ):
            raise ValueError("R03 Connector dispatch did not fail closed")
        return {
            "request_id": request_id, "status": 409, "error_code": "cluster_not_ready",
        }


class GatewayDependencyProbe:
    """Normalizes actor-scoped Gateway, Console, Connector, and Loki facts."""

    def __init__(
        self,
        *,
        base_url: str,
        user: UserSession,
        admin: UserSession,
        browser: DependencyBrowser,
        connector_poll: ConnectorPollProbe,
        loki: LokiDependencyProbe,
        sre_username: str,
        sre_password: str | CredentialValue,
        admin_username: str,
        admin_password: str | CredentialValue,
    ) -> None:
        self.base_url = base_url
        self.user = user
        self.admin = admin
        self.browser = browser
        self.connector_poll = connector_poll
        self.loki = loki
        self.sre_username = sre_username
        self.sre_password = sre_password
        self.admin_username = admin_username
        self.admin_password = admin_password

    def snapshot_ready(
        self, scope: RecoveryScope, target: RecoveryTarget,
    ) -> dict[str, object]:
        capability = self._capability(
            "observability" if target.owner == "loki" else target.owner
        )
        availability = capability.get("availability")
        if (
            capability.get("readiness") != "ready"
            or not isinstance(availability, dict)
            or availability.get("state") != "available"
        ):
            raise ValueError(f"Dependency {target.owner} is not publicly ready")
        if target.owner == "connector":
            state = self._connector_cluster(scope)
            verification = state.get("read_verification")
            if (
                state.get("connector_id") != scope.connector_id
                or state.get("runtime_status") != "online"
                or not isinstance(verification, dict)
                or verification.get("status") != "verified"
                or verification.get("cluster_identity") != {
                    "connector_id": scope.connector_id, "cluster_id": scope.cluster_id,
                }
            ):
                raise ValueError("Connector heartbeat/read verification is not ready")
            return {
                "availability": "available", "reason_code": "ready",
                "connector_id": scope.connector_id, "cluster_id": scope.cluster_id,
                "heartbeat": "online", "read_verification": "verified",
            }
        return {
            "availability": "available", "reason_code": "ready",
            "observability_readiness": "ready", "loki_state": "available",
        }

    def prepare_connector_probe(
        self, scope: RecoveryScope, *, operation_id: str,
    ) -> dict[str, object]:
        browser = self.browser.prepare_r03(
            base_url=self.base_url,
            username=self.sre_username,
            password=self.sre_password,
            incident_id=scope.incident_id,
            connector_id=scope.connector_id,
            cluster_id=scope.cluster_id,
            operation_id=operation_id,
        )
        self._require_browser(browser, "r03_prepare")
        prepared = browser.summary.get("prepared")
        if not isinstance(prepared, dict):
            raise ValueError("R03 Console preparation omitted its Change identities")
        mutations = browser.summary.get("mutations")
        expected_path = f"/api/v1/incidents/{scope.incident_id}/change-requests"
        mutation = mutations[0] if isinstance(mutations, list) and len(mutations) == 1 else None
        identities = mutation.get("identities") if isinstance(mutation, dict) else None
        if (
            not isinstance(mutation, dict)
            or mutation.get("method") != "POST"
            or mutation.get("path") != expected_path
            or mutation.get("status") != 201
            or not isinstance(identities, dict)
            or prepared.get("change_request_id") not in identities.values()
        ):
            raise ValueError("R03 Console preparation was not durably bound")
        result = {"status": "succeeded", "operation_id": operation_id, **prepared}
        assert_public_payload(result)
        return result

    def reconcile_connector_probe(
        self, scope: RecoveryScope, *, operation_id: str,
    ) -> dict[str, object] | None:
        return None

    def verify_connector_unavailable(
        self,
        scope: RecoveryScope,
        prepared: dict[str, object],
        *,
        operation_id: str,
    ) -> dict[str, object]:
        platform = self._unavailable_capability("connector")
        loki = self.snapshot_ready(
            scope, RecoveryTarget("loki", "deployment", "aiops-loki"),
        )
        admin = self.browser.verify_r03_admin_denial(
            base_url=self.base_url,
            username=self.admin_username,
            password=self.admin_password,
            cluster_id=scope.cluster_id,
            operation_id=operation_id,
        )
        sre = self.browser.verify_r03_sre_denials(
            base_url=self.base_url,
            username=self.sre_username,
            password=self.sre_password,
            incident_id=scope.incident_id,
            prepared=prepared,
            operation_id=operation_id,
        )
        self._require_browser(admin, "r03_admin")
        self._require_browser(sre, "r03_sre")
        live_evidence = admin.summary.get("live_evidence")
        sre_denials = sre.summary.get("denials")
        approval = sre.summary.get("approval")
        if (
            not isinstance(live_evidence, dict)
            or not isinstance(sre_denials, dict)
            or not isinstance(approval, dict)
        ):
            raise ValueError("R03 Console denial probes are incomplete")
        self._require_mutations(
            admin,
            [("POST", "/api/v1/admin/connector-commands", live_evidence)],
        )
        root = f"/api/v1/change-requests/{prepared.get('change_request_id')}"
        self._require_mutations(sre, [
            (
                "POST", f"/api/v1/incidents/{scope.incident_id}/change-requests",
                sre_denials.get("dry_run"),
            ),
            ("POST", f"{root}/phase-approval/approve", approval),
            ("POST", f"{root}/phase-execution/start", sre_denials.get("grant")),
        ])
        dispatch = self.connector_poll.unavailable(
            scope, request_id=f"{operation_id}/dispatch",
        )
        result = {
            "status": "succeeded", "operation_id": operation_id,
            "dependency": "connector", "platform": platform,
            "denials": {
                "live_evidence": live_evidence,
                "dry_run": sre_denials.get("dry_run"),
                "grant": sre_denials.get("grant"),
                "dispatch": dispatch,
            },
            "active_commands": sre.summary.get("active_commands"),
            "grants_created": sre.summary.get("grants_created"),
            "prepared_change_request_id": prepared.get("change_request_id"),
            "approval": approval,
            "other_dependency": {"owner": "loki", **loki},
        }
        assert_public_payload(result)
        return result

    def reconcile_connector_unavailable(
        self,
        scope: RecoveryScope,
        prepared: dict[str, object],
        *,
        operation_id: str,
    ) -> dict[str, object] | None:
        return None

    def verify_connector_recovered(
        self, scope: RecoveryScope, *, operation_id: str,
    ) -> dict[str, object]:
        ready = self.snapshot_ready(scope, RecoveryTarget("connector", "deployment", "aiops-connector"))
        return {
            "status": "succeeded", "operation_id": operation_id,
            "dependency": "connector", **ready,
        }

    def reconcile_connector_recovered(
        self, scope: RecoveryScope, *, operation_id: str,
    ) -> dict[str, object] | None:
        return None

    def verify_loki_unavailable(
        self, scope: RecoveryScope, *, operation_id: str,
    ) -> dict[str, object]:
        connector = self.snapshot_ready(
            scope, RecoveryTarget("connector", "deployment", "aiops-connector"),
        )
        result = {
            "status": "succeeded", "operation_id": operation_id,
            "dependency": "loki", "platform": self._unavailable_capability("observability"),
            "mcp": self.loki.unavailable(scope, request_id=operation_id),
            "other_dependency": {"owner": "connector", **connector},
        }
        assert_public_payload(result)
        return result

    def reconcile_loki_unavailable(
        self, scope: RecoveryScope, *, operation_id: str,
    ) -> dict[str, object] | None:
        return None

    def verify_loki_recovered(
        self, scope: RecoveryScope, *, operation_id: str,
    ) -> dict[str, object]:
        ready = self.snapshot_ready(scope, RecoveryTarget("loki", "deployment", "aiops-loki"))
        result = {
            "status": "succeeded", "operation_id": operation_id,
            "dependency": "loki", "availability": ready["availability"],
            **self.loki.recovered(scope, request_id=operation_id),
        }
        assert_public_payload(result)
        return result

    def reconcile_loki_recovered(
        self, scope: RecoveryScope, *, operation_id: str,
    ) -> dict[str, object] | None:
        return None

    def _capability(self, name: str) -> dict[str, object]:
        response = self.user.request("GET", "/api/v1/platform/status")
        capabilities = response.body.get("capabilities") if response.status == 200 else None
        value = capabilities.get(name) if isinstance(capabilities, dict) else None
        if not isinstance(value, dict):
            raise ValueError(f"Platform Status omitted {name} capability")
        return value

    def _unavailable_capability(self, name: str) -> dict[str, str]:
        capability = self._capability(name)
        availability = capability.get("availability")
        if (
            capability.get("readiness") != "not_ready"
            or not isinstance(availability, dict)
            or availability.get("state") != "unavailable"
            or not availability.get("reason_code")
        ):
            raise ValueError(f"Platform Status did not truthfully degrade {name}")
        return {
            "availability": "unavailable",
            "reason_code": str(availability["reason_code"]),
        }

    def _connector_cluster(self, scope: RecoveryScope) -> dict[str, object]:
        response = self.admin.request("GET", "/api/v1/admin/connector-enrollments")
        clusters = response.body.get("clusters") if response.status == 200 else None
        matches = [
            item for item in clusters
            if isinstance(item, dict) and item.get("cluster_id") == scope.cluster_id
        ] if isinstance(clusters, list) else []
        if len(matches) != 1:
            raise ValueError("Connector Cluster public identity is not unique")
        return matches[0]

    @staticmethod
    def _require_browser(result: BrowserResult, action: str) -> None:
        if (
            result.summary.get("action") != action
            or result.summary.get("same_origin") is not True
            or result.summary.get("screenshots_masked") is not True
            or set(result.screenshots) != {f"{action}.png"}
        ):
            raise ValueError(f"R03 {action} browser evidence is invalid")

    @staticmethod
    def _require_mutations(
        result: BrowserResult,
        expected: list[tuple[str, str, object]],
    ) -> None:
        mutations = result.summary.get("mutations")
        if not isinstance(mutations, list) or len(mutations) != len(expected):
            raise ValueError("R03 Console mutations were not durably bound")
        for mutation, (method, path, summary) in zip(mutations, expected, strict=True):
            if not isinstance(mutation, dict) or not isinstance(summary, dict):
                raise ValueError("R03 Console mutation summary is invalid")
            if (
                mutation.get("method") != method
                or mutation.get("path") != path
                or any(
                    mutation.get(field) != summary.get(field)
                    for field in (
                        "request_id", "status", "response_request_id", "error_code",
                    )
                )
                or (
                    isinstance(mutation.get("status"), int)
                    and 200 <= mutation["status"] < 300
                    and (
                        not isinstance(mutation.get("identities"), dict)
                        or not mutation["identities"]
                    )
                )
            ):
                raise ValueError("R03 Console mutation identity is not reconciled")
