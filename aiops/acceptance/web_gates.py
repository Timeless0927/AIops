"""A01 public access and first-login security gates."""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from typing import Callable, Protocol

from .evidence import AcceptanceEvidence, Artifact
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
        started_at = self.evidence.start_gate("I05")
        self.evidence.require_verified_attestation(
            "I05", role="platform_administrator"
        )
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
            browser = self.browser.probe(
                self.anonymous.base_url,
                username=admin_username,
                password=admin_password,
            )
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

    @staticmethod
    def _safe_actor(actor: dict[str, object]) -> dict[str, object]:
        return {
            "id": actor.get("id"),
            "username": actor.get("username"),
            "roles": actor.get("roles"),
            "capabilities": actor.get("capabilities"),
            "is_platform_administrator": actor.get("is_platform_administrator"),
        }
