"""A01 public access and first-login security gates."""

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Callable, Protocol

from .browser_mutations import reconcile_unique_browser_operation
from .evidence import AcceptanceEvidence
from .evidence_types import Artifact
from .http import GatewaySession
from .integration_support import fail_gate


@dataclass(frozen=True)
class BrowserResult:
    summary: dict[str, object]
    screenshots: dict[str, bytes]


class BrowserProbe(Protocol):
    def probe(
        self,
        base_url: str,
        *,
        username: str | None = None,
        password: str | None = None,
    ) -> BrowserResult: ...

    def provision_i05_user(
        self,
        base_url: str,
        *,
        admin_username: str,
        admin_password: str,
        user_username: str,
        user_password: str,
        evidence: AcceptanceEvidence,
    ) -> BrowserResult: ...


def assert_same_origin_browser(result: BrowserResult, base_url: str) -> None:
    if result.summary.get("same_origin") is not True:
        raise ValueError("browser observed a cross-origin product request")
    expected_origin = urllib.parse.urlsplit(base_url)
    origin = f"{expected_origin.scheme}://{expected_origin.netloc}"
    if any(item != origin for item in result.summary.get("origins", [])):
        raise ValueError("browser network summary contains a non-Gateway origin")
    paths = set(result.summary.get("paths", []))
    if not any(str(path).startswith("/assets/") for path in paths):
        raise ValueError("browser did not load Console static assets")
    if not {"/auth/login", "/api/v1/actor"} <= paths:
        raise ValueError("browser did not exercise same-origin auth and API routes")


class WebGateRunner:
    def __init__(
        self,
        *,
        evidence: AcceptanceEvidence,
        anonymous: GatewaySession,
        session_factory: Callable[[], GatewaySession],
        browser: BrowserProbe,
    ) -> None:
        self.evidence = evidence
        self.anonymous = anonymous
        self.session_factory = session_factory
        self.browser = browser

    def run_i03(
        self,
        base_url: str,
        *,
        admin_username: str,
        admin_password: str,
    ) -> None:
        started_at = self.evidence.start_gate("I03")
        artifacts: list[Artifact] = []
        try:
            parsed = urllib.parse.urlsplit(base_url)
            if parsed.scheme != "http" or parsed.port != 30088 or parsed.path not in {"", "/"}:
                raise ValueError("I03 requires canonical http://<NodeIP>:30088")
            health = self.anonymous.request("GET", "/healthz")
            actor = self.anonymous.request("GET", "/api/v1/actor")
            stream = self.anonymous.request(
                "GET", "/api/v1/platform/status/stream"
            )
            if health.status != 200 or actor.status != 401 or stream.status != 401:
                raise ValueError("mandatory health/auth/event-stream HTTP matrix failed")
            browser = self.browser.probe(
                base_url, username=admin_username, password=admin_password
            )
            assert_same_origin_browser(browser, base_url)
            authenticated_stream = browser.summary.get("authenticated_event_stream_status")
            authenticated_stream_type = str(
                browser.summary.get("authenticated_event_stream_content_type") or ""
            ).lower()
            if authenticated_stream != 200 or not authenticated_stream_type.startswith(
                "text/event-stream"
            ):
                raise ValueError("browser did not exercise an authenticated same-origin SSE route")
            artifacts.extend(
                [
                    self.evidence.write_json(
                        "I03",
                        "http-matrix.json",
                        {
                            "healthz": health.status,
                            "anonymous_actor": actor.status,
                            "anonymous_event_stream": stream.status,
                            "authenticated_event_stream": authenticated_stream,
                            "authenticated_event_stream_content_type": browser.summary.get(
                                "authenticated_event_stream_content_type"
                            ),
                        },
                    ),
                    self.evidence.write_json(
                        "I03",
                        "browser-network.json",
                        self.evidence.contextualize(browser.summary),
                    ),
                ]
            )
            artifacts.extend(
                self.evidence.write_bytes("I03", name, content)
                for name, content in sorted(browser.screenshots.items())
            )
            self.evidence.record_gate("I03", "passed", artifacts, started_at=started_at)
        except Exception as exc:
            fail_gate(self.evidence, "I03", artifacts, exc, (admin_password,), started_at)

    def run_i04(
        self,
        https_base_url: str | None,
        profile_identity: dict[str, object] | None = None,
    ) -> None:
        started_at = self.evidence.start_gate("I04")
        artifacts: list[Artifact] = []
        if self.evidence.access_profile == "http_nodeport":
            artifact = self.evidence.write_json(
                "I04",
                "https-profile.json",
                {"declared": False, "reason": "release acceptance profile is http_nodeport"},
            )
            self.evidence.record_gate(
                "I04", "not_applicable", [artifact], started_at=started_at
            )
            return
        try:
            if not https_base_url or urllib.parse.urlsplit(https_base_url).scheme != "https":
                raise ValueError("declared https_ingress profile requires an HTTPS URL")
            if (
                not profile_identity
                or not profile_identity.get("ingress")
                or not profile_identity.get("tls")
            ):
                raise ValueError("declared https_ingress profile requires Ingress/TLS identity")
            browser = self.browser.probe(https_base_url)
            assert_same_origin_browser(browser, https_base_url)
            artifacts.append(
                self.evidence.write_json(
                    "I04",
                    "browser-network.json",
                    self.evidence.contextualize(browser.summary),
                )
            )
            artifacts.append(
                self.evidence.write_json(
                    "I04",
                    "ingress-tls-identity.json",
                    {
                        "release_sha256": self.evidence.candidate_sha256,
                        "kube_context": self.evidence.kube_context,
                        **profile_identity,
                    },
                )
            )
            artifacts.extend(
                self.evidence.write_bytes("I04", name, content)
                for name, content in sorted(browser.screenshots.items())
            )
            self.evidence.record_gate("I04", "passed", artifacts, started_at=started_at)
        except Exception as exc:
            fail_gate(self.evidence, "I04", artifacts, exc, (), started_at)

    def run_i05(
        self,
        *,
        admin_username: str,
        admin_password: str,
        user_username: str,
        user_password: str,
    ) -> None:
        self.evidence.require_verified_attestation(
            "I05", role="platform_administrator"
        )
        started_at = self.evidence.start_gate("I05")
        secrets = (admin_password, user_password)
        artifacts: list[Artifact] = []
        try:
            wrong = self.session_factory()
            wrong_login = wrong.request(
                "POST",
                "/auth/login",
                body={
                    "username": admin_username,
                    "password": "definitely-wrong-password",
                    "session_mode": "cookie",
                },
                csrf=False,
            )
            admin = self.session_factory()
            admin_login = admin.request(
                "POST",
                "/auth/login",
                body={"username": admin_username, "password": admin_password, "session_mode": "cookie"},
                csrf=False,
            )
            admin_actor = admin.request("GET", "/api/v1/actor")
            csrf = admin.request("GET", "/auth/csrf")
            logout_without_csrf = admin.request("POST", "/auth/logout", body={}, csrf=False)
            browser = self.browser.provision_i05_user(
                self.anonymous.base_url,
                admin_username=admin_username,
                admin_password=admin_password,
                user_username=user_username,
                user_password=user_password,
                evidence=self.evidence,
            )
            mutations = browser.summary.get("mutations")
            if (
                not isinstance(mutations, list)
                or len(mutations) != 1
                or not isinstance(mutations[0], Mapping)
                or any(
                    mutations[0].get(key) != value
                    for key, value in {
                        "method": "POST",
                        "path": "/api/v1/admin/users",
                        "status": 201,
                    }.items()
                )
            ):
                raise ValueError("ordinary User was not created through the Console")
            user = self.session_factory()
            user_login = user.request(
                "POST",
                "/auth/login",
                body={"username": user_username, "password": user_password, "session_mode": "cookie"},
                csrf=False,
            )
            user_actor = user.request("GET", "/api/v1/actor")
            if [
                wrong_login.status,
                admin_login.status,
                admin_actor.status,
                csrf.status,
                logout_without_csrf.status,
                user_login.status,
                user_actor.status,
            ] != [401, 200, 200, 200, 403, 200, 200]:
                raise ValueError("login/session/CSRF status matrix failed")
            admin_value = admin_actor.body["actor"]
            user_value = user_actor.body["actor"]
            if not admin_value.get("is_platform_administrator") or user_value.get(
                "is_platform_administrator"
            ):
                raise ValueError("Platform Administrator and ordinary User roles are not distinct")
            assert_same_origin_browser(browser, self.anonymous.base_url)
            artifacts.extend(
                [
                    self.evidence.write_json(
                        "I05",
                        "auth-matrix.json",
                        {
                            "wrong_password": wrong_login.status,
                            "admin_login": admin_login.status,
                            "csrf": csrf.status,
                            "mutation_without_csrf": logout_without_csrf.status,
                            "ordinary_user_creation": mutations[0]["status"],
                            "ordinary_user_login": user_login.status,
                        },
                        known_secrets=secrets,
                    ),
                    self.evidence.write_json(
                        "I05",
                        "role-boundary.json",
                        {
                            "administrator": self._safe_actor(admin_value),
                            "ordinary_user": self._safe_actor(user_value),
                        },
                        known_secrets=secrets,
                    ),
                    self.evidence.write_json(
                        "I05",
                        "authenticated-browser-network.json",
                        self.evidence.contextualize(browser.summary),
                    ),
                ]
            )
            self.evidence.record_gate("I05", "passed", artifacts, started_at=started_at)
        except Exception as exc:
            fail_gate(self.evidence, "I05", artifacts, exc, secrets, started_at)

    def resume_i05(
        self,
        *,
        admin_username: str,
        admin_password: str,
        user_username: str,
        user_password: str,
    ) -> dict[str, str]:
        execution = self.evidence.resume_gate("I05")
        artifacts = list(execution.artifacts)
        secrets = (admin_password, user_password)
        try:
            self.evidence.require_verified_attestation(
                "I05", role="platform_administrator"
            )
            mutations = [
                item for item in execution.operations
                if item.get("kind") == "console_mutation"
            ]
            if (
                len(mutations) != 1
                or any(
                    item.get("kind") not in {"gate_execution", "console_mutation"}
                    for item in execution.operations
                )
            ):
                raise ValueError(
                    "I05 interrupted without one durable Console mutation intent; "
                    "the effect was not replayed"
                )
            operation_id = str(mutations[0]["operation_id"])

            wrong = self.session_factory()
            wrong_login = wrong.request(
                "POST", "/auth/login",
                body={
                    "username": admin_username,
                    "password": "definitely-wrong-password",
                    "session_mode": "cookie",
                },
                csrf=False,
            )
            admin = self.session_factory()
            admin_login = admin.request(
                "POST", "/auth/login",
                body={
                    "username": admin_username,
                    "password": admin_password,
                    "session_mode": "cookie",
                },
                csrf=False,
            )
            admin_actor = admin.request("GET", "/api/v1/actor")
            csrf = admin.request("GET", "/auth/csrf")
            logout_without_csrf = admin.request(
                "POST", "/auth/logout", body={}, csrf=False,
            )
            if admin_login.status != 200 or admin_actor.status != 200:
                raise ValueError("I05 resume could not authenticate the Platform Administrator")
            admin_value = admin_actor.body["actor"]
            audit = admin.request("GET", "/api/v1/admin/audit")
            if audit.status != 200 or not isinstance(audit.body, Mapping):
                raise ValueError("I05 resume could not read public admin audit facts")
            rows = audit.body.get("audit")
            if not isinstance(rows, list):
                raise ValueError("I05 resume admin audit projection is invalid")
            matches = self._i05_audit_facts(
                rows,
                operation_id=operation_id,
                actor_id=str(admin_value.get("id") or ""),
                username=user_username,
            )
            reconciliations = {
                str(item.get("operation_id")): item
                for item in execution.reconciliations
            }
            existing = reconciliations.get(operation_id)
            if existing is None:
                mutation_fact = reconcile_unique_browser_operation(
                    self.evidence, "I05", operation_id,
                    lambda _request_id: matches,
                )
            elif existing.get("outcome") == "succeeded" and len(matches) == 1:
                mutation_fact = matches[0]
                public_fact = existing.get("public_fact")
                if not isinstance(public_fact, Mapping):
                    raise ValueError("I05 browser reconciliation fact is invalid")
                identities = public_fact.get("identities")
                identity_fact = identities if isinstance(identities, Mapping) else public_fact
                if any(
                    str(identity_fact.get(key)) != str(mutation_fact[key])
                    for key in ("user.id", "user.updated_at")
                ):
                    raise ValueError("I05 resumed User identity drifted from browser result")
            else:
                raise ValueError("I05 interrupted User creation outcome is unprovable")

            browser = self.browser.probe(
                self.anonymous.base_url,
                username=admin_username,
                password=admin_password,
            )
            browser.summary["mutations"] = [{
                "request_id": operation_id,
                "method": "POST",
                "path": "/api/v1/admin/users",
                "status": 201,
                "response_request_id": operation_id,
                "identities": {
                    "user.id": mutation_fact["user.id"],
                    "user.updated_at": mutation_fact["user.updated_at"],
                },
            }]
            assert_same_origin_browser(browser, self.anonymous.base_url)

            user = self.session_factory()
            user_login = user.request(
                "POST", "/auth/login",
                body={
                    "username": user_username,
                    "password": user_password,
                    "session_mode": "cookie",
                },
                csrf=False,
            )
            user_actor = user.request("GET", "/api/v1/actor")
            if [
                wrong_login.status,
                csrf.status,
                logout_without_csrf.status,
                user_login.status,
                user_actor.status,
            ] != [401, 200, 403, 200, 200]:
                raise ValueError("I05 resumed login/session/CSRF status matrix failed")
            user_value = user_actor.body["actor"]
            if not admin_value.get("is_platform_administrator") or user_value.get(
                "is_platform_administrator"
            ):
                raise ValueError("Platform Administrator and ordinary User roles are not distinct")

            indexed = {item.path.name for item in artifacts}
            values = {
                "auth-matrix.json": {
                    "wrong_password": wrong_login.status,
                    "admin_login": admin_login.status,
                    "csrf": csrf.status,
                    "mutation_without_csrf": logout_without_csrf.status,
                    "ordinary_user_creation": 201,
                    "ordinary_user_login": user_login.status,
                },
                "role-boundary.json": {
                    "administrator": self._safe_actor(admin_value),
                    "ordinary_user": self._safe_actor(user_value),
                },
                "authenticated-browser-network.json": self.evidence.contextualize(
                    browser.summary
                ),
            }
            for name, value in values.items():
                if name not in indexed:
                    artifacts.append(
                        self.evidence.write_json(
                            "I05", name, value, known_secrets=secrets,
                        )
                    )

            execution_reconciliation = reconciliations.get(execution.execution_id)
            if execution_reconciliation is None:
                self.evidence.reconcile_operation(
                    "I05",
                    operation_id=execution.execution_id,
                    outcome="succeeded",
                    public_fact={
                        "request_id": operation_id,
                        "user_id": mutation_fact["user.id"],
                        "terminal": True,
                    },
                )
            elif execution_reconciliation.get("outcome") != "succeeded":
                raise ValueError("I05 gate execution reconciliation is not successful")
            self.evidence.record_gate(
                "I05", "passed", artifacts, started_at=execution.started_at,
            )
            return {"gate_id": "I05", "status": "passed"}
        except Exception as exc:
            fail_gate(
                self.evidence, "I05", artifacts, exc, secrets, execution.started_at,
            )

    @staticmethod
    def _i05_audit_facts(
        rows: list[object], *, operation_id: str, actor_id: str, username: str,
    ) -> list[dict[str, object]]:
        facts: list[dict[str, object]] = []
        for row in rows:
            if not isinstance(row, Mapping) or row.get("request_id") != operation_id:
                continue
            after = row.get("after")
            if (
                row.get("actor_id") != actor_id
                or row.get("target_type") != "users"
                or row.get("action") != "users_create"
                or row.get("result") != "success"
                or not isinstance(after, Mapping)
                or after.get("username") != username
                or after.get("id") != row.get("target_id")
                or after.get("updated_at") is None
            ):
                continue
            facts.append({
                "request_id": operation_id,
                "user.id": str(after["id"]),
                "user.updated_at": str(after["updated_at"]),
            })
        return facts

    @staticmethod
    def _safe_actor(actor: dict[str, object]) -> dict[str, object]:
        return {
            "id": actor.get("id"),
            "username": actor.get("username"),
            "roles": actor.get("roles"),
            "capabilities": actor.get("capabilities"),
            "is_platform_administrator": actor.get("is_platform_administrator"),
        }
