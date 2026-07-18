from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from aiops.acceptance.evidence import A01_GATE_SEQUENCE, AcceptanceEvidence, EvidenceError
from aiops.acceptance.http import HttpResponse
from aiops.acceptance.evidence import GateFailed
from tests.pilot_acceptance_support import create_evidence
from aiops.acceptance.web_gates import BrowserResult, WebGateRunner


ADMIN_PASSWORD = "admin-super-secret"
USER_PASSWORD = "user-super-secret"


def _evidence(tmp_path: Path, profile: str = "http_nodeport") -> AcceptanceEvidence:
    return create_evidence(
        tmp_path / "acceptance",
        acceptance_id=f"v0.1.0-web-{profile}",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        acceptance_tool_sha256="c" * 64,
        gate_contract_revision="pilot-clean-acceptance-v3",
        kube_context="clean",
        cluster_identity_sha256="b" * 64,
        access_profile=profile,
        now=lambda: "2026-07-14T01:02:03Z",
        attestation_verifier=lambda _item: None,
    )


class FakeBrowser:
    def probe(self, base_url: str, *, username=None, password=None) -> BrowserResult:
        assert password in {None, ADMIN_PASSWORD}
        return BrowserResult(
            summary={
                "base_url": base_url,
                "same_origin": True,
                "origins": [base_url],
                "paths": ["/", "/assets/index.js", "/auth/login", "/api/v1/actor"],
                "authenticated_event_stream_status": 200 if username else None,
                "authenticated_event_stream_content_type": "text/event-stream; charset=utf-8" if username else None,
            },
            screenshots={"desktop.png": b"\x89PNG-desktop", "mobile.png": b"\x89PNG-mobile"},
        )

    def provision_i05_user(
        self,
        base_url: str,
        *,
        admin_username: str,
        admin_password: str,
        user_username: str,
        user_password: str,
        evidence: AcceptanceEvidence,
    ) -> BrowserResult:
        assert (admin_username, admin_password) == ("admin", ADMIN_PASSWORD)
        assert (user_username, user_password) == ("sre-user", USER_PASSWORD)
        result = self.probe(base_url, username=admin_username, password=admin_password)
        result.summary["mutations"] = [{
            "request_id": "i05-create-user",
            "method": "POST",
            "path": "/api/v1/admin/users",
            "status": 201,
            "response_request_id": "i05-create-user",
            "identities": {"user.id": "ordinary-user"},
        }]
        return result


class FakeSession:
    def __init__(self, role: str = "anonymous") -> None:
        self.role = role
        self.csrf = "csrf-secret"
        self.base_url = "http://192.0.2.10:30088"

    def request(self, method: str, path: str, *, body=None, csrf=True, request_id=None):
        if path == "/healthz":
            return HttpResponse(200, "ok", {"content-type": "text/plain"})
        if path in {"/api/v1/actor", "/api/v1/incidents", "/api/v1/platform/status/stream"} and self.role == "anonymous":
            return HttpResponse(401, {"error": {"code": "unauthorized"}}, {})
        if path == "/auth/login":
            password = body["password"]
            if password == "definitely-wrong-password":
                return HttpResponse(401, {"error": {"code": "invalid_credentials"}}, {})
            if password == ADMIN_PASSWORD and body["username"] == "admin":
                self.role = "admin"
            elif password == USER_PASSWORD and body["username"] == "sre-user":
                self.role = "user"
            else:
                return HttpResponse(401, {"error": {"code": "invalid_credentials"}}, {})
            return HttpResponse(200, {"actor": self._actor()}, {})
        if path == "/api/v1/actor":
            return HttpResponse(200, {"actor": self._actor()}, {})
        if path == "/auth/csrf":
            return HttpResponse(200, {"csrf_token": self.csrf}, {})
        if path == "/auth/logout" and not csrf:
            return HttpResponse(403, {"error": {"code": "csrf_required"}}, {})
        raise AssertionError((method, path, body, csrf, request_id))

    def _actor(self):
        admin = self.role == "admin"
        return {
            "id": self.role,
            "username": "admin" if admin else "sre-user",
            "roles": ["platform_administrator" if admin else "sre"],
            "capabilities": ["admin"] if admin else ["read"],
            "is_platform_administrator": admin,
        }


def _attest_login(evidence: AcceptanceEvidence) -> None:
    statement = evidence.attestation_statement(
        actor="admin",
        role="platform_administrator",
        gate_ids=["I05"],
        conclusion="passed",
        note="first login observed in the browser",
    )
    evidence.append_attestation(
        statement,
        signature="signature",
        public_key="ssh-ed25519 AAAATEST admin",
        fingerprint="SHA256:test",
    )


def _advance(evidence: AcceptanceEvidence, gate_id: str) -> None:
    for predecessor in A01_GATE_SEQUENCE[: A01_GATE_SEQUENCE.index(gate_id)]:
        evidence.start_gate(predecessor)
        evidence.record_gate(
            predecessor,
            (
                "not_applicable"
                if predecessor == "I04" and evidence.access_profile == "http_nodeport"
                else "passed"
            ),
            [],
        )


def test_i03_and_http_profile_i04_record_browser_same_origin_evidence(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    _advance(evidence, "I03")
    runner = WebGateRunner(
        evidence=evidence,
        anonymous=FakeSession(),
        session_factory=FakeSession,
        browser=FakeBrowser(),
    )

    runner.run_i03(
        "http://192.0.2.10:30088",
        admin_username="admin",
        admin_password=ADMIN_PASSWORD,
    )
    runner.run_i04(None)

    manifest = json.loads(evidence.manifest_path.read_text())
    assert manifest["gates"]["I03"][0]["status"] == "passed"
    assert manifest["gates"]["I04"][0]["status"] == "not_applicable"
    names = {
        Path(item["path"]).name for item in manifest["gates"]["I03"][0]["artifacts"]
    }
    assert {"browser-network.json", "desktop.png", "mobile.png", "http-matrix.json"} <= names


def test_web_gate_cannot_run_before_install_frontier(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    runner = WebGateRunner(
        evidence=evidence,
        anonymous=FakeSession(),
        session_factory=FakeSession,
        browser=FakeBrowser(),
    )
    with pytest.raises(EvidenceError, match="not frontier"):
        runner.run_i03(
            "http://192.0.2.10:30088",
            admin_username="admin",
            admin_password=ADMIN_PASSWORD,
        )


def test_i03_rejects_authenticated_non_sse_response(tmp_path: Path) -> None:
    class NonSseBrowser(FakeBrowser):
        def probe(self, base_url: str, *, username=None, password=None) -> BrowserResult:
            result = super().probe(
                base_url, username=username, password=password
            )
            result.summary["authenticated_event_stream_status"] = 404
            result.summary["authenticated_event_stream_content_type"] = "application/json"
            return result

    evidence = _evidence(tmp_path)
    _advance(evidence, "I03")
    runner = WebGateRunner(
        evidence=evidence,
        anonymous=FakeSession(),
        session_factory=FakeSession,
        browser=NonSseBrowser(),
    )
    with pytest.raises(GateFailed, match="I03"):
        runner.run_i03(
            "http://192.0.2.10:30088",
            admin_username="admin",
            admin_password=ADMIN_PASSWORD,
        )


def test_https_profile_requires_real_https_browser_probe(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path, "https_ingress")
    _advance(evidence, "I04")
    runner = WebGateRunner(
        evidence=evidence,
        anonymous=FakeSession(),
        session_factory=FakeSession,
        browser=FakeBrowser(),
    )
    with pytest.raises(GateFailed, match="I04"):
        runner.run_i04(None)

    passing = _evidence(tmp_path / "passing", "https_ingress")
    _advance(passing, "I04")
    WebGateRunner(
        evidence=passing,
        anonymous=FakeSession(),
        session_factory=FakeSession,
        browser=FakeBrowser(),
    ).run_i04(
        "https://console.example.test",
        {
            "ingress": {"namespace": "edge", "name": "aiops", "uid": "ingress-uid"},
            "tls": {"certificate_sha256": "c" * 64, "not_after": "2030"},
            "redirect": {"declared": False},
        },
    )
    assert json.loads(passing.manifest_path.read_text())["gates"]["I04"][0]["status"] == "passed"


def test_i05_records_auth_and_role_matrix_without_password_cookie_or_csrf(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    _advance(evidence, "I05")
    _attest_login(evidence)
    runner = WebGateRunner(
        evidence=evidence,
        anonymous=FakeSession(),
        session_factory=FakeSession,
        browser=FakeBrowser(),
    )

    runner.run_i05(
        admin_username="admin",
        admin_password=ADMIN_PASSWORD,
        user_username="sre-user",
        user_password=USER_PASSWORD,
    )

    manifest = json.loads(evidence.manifest_path.read_text())
    assert manifest["gates"]["I05"][0]["status"] == "passed"
    persisted = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in evidence.root.rglob("*")
        if path.is_file()
    )
    assert ADMIN_PASSWORD not in persisted
    assert USER_PASSWORD not in persisted
    assert "csrf-secret" not in persisted
    matrix = json.loads(
        next(evidence.root.rglob("auth-matrix.json")).read_text(encoding="utf-8")
    )
    assert matrix["ordinary_user_creation"] == 201


def test_i05_rejects_malformed_browser_mutation_facts(tmp_path: Path) -> None:
    class MalformedBrowser(FakeBrowser):
        def provision_i05_user(self, *args, **kwargs) -> BrowserResult:
            result = super().provision_i05_user(*args, **kwargs)
            result.summary["mutations"] = [None]
            return result

    evidence = _evidence(tmp_path)
    _advance(evidence, "I05")
    _attest_login(evidence)
    runner = WebGateRunner(
        evidence=evidence,
        anonymous=FakeSession(),
        session_factory=FakeSession,
        browser=MalformedBrowser(),
    )

    with pytest.raises(GateFailed, match="I05"):
        runner.run_i05(
            admin_username="admin",
            admin_password=ADMIN_PASSWORD,
            user_username="sre-user",
            user_password=USER_PASSWORD,
        )
