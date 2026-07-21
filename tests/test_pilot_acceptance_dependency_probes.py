from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from aiops.acceptance.credentials import CredentialValue
from aiops.acceptance.dependency_degradation import R03_TARGET, R04_TARGET
from aiops.acceptance.dependency_probes import ConnectorPollGatewayProbe, GatewayDependencyProbe
from aiops.acceptance.http import HttpResponse
from aiops.acceptance.recovery import load_recovery_scope
from aiops.acceptance.web_gates import BrowserResult
from tests.pilot_acceptance_recovery_support import recovery_ledger


class Session:
    def __init__(self, *, admin: bool = False) -> None:
        self.admin = admin
        self.unavailable: set[str] = set()

    def request(self, method: str, path: str, **_kwargs) -> HttpResponse:
        assert method == "GET"
        if path == "/api/v1/platform/status":
            capabilities = {
                name: {
                    "readiness": (
                            "not_ready" if name in self.unavailable else "ready"
                    ),
                    "availability": {
                        "state": (
                            "unavailable" if name in self.unavailable else "available"
                        ),
                        "reason_code": (
                            "owner_unavailable" if name in self.unavailable else None
                        ),
                    },
                }
                for name in ("connector", "observability")
            }
            return HttpResponse(200, {"capabilities": capabilities}, {})
        assert self.admin and path == "/api/v1/admin/connector-enrollments"
        return HttpResponse(200, {"clusters": [{
            "connector_id": "connector-prod", "cluster_id": "pilot-cluster",
            "runtime_status": "online",
            "read_verification": {
                "status": "verified",
                "cluster_identity": {
                    "connector_id": "connector-prod", "cluster_id": "pilot-cluster",
                },
            },
        }]}, {})


def _browser(action: str, **summary) -> BrowserResult:
    return BrowserResult(
        {
            "action": action, "same_origin": True, "screenshots_masked": True,
            **summary,
        },
        {f"{action}.png": b"masked"},
    )


class Browser:
    def prepare_r03(self, **_kwargs) -> BrowserResult:
        prepared = {
            "change_request_id": "change-probe", "phase_id": "phase-probe",
            "revision_id": "revision-probe", "dry_run_hash": "d" * 64,
            "target_confirmation": "apps/v1:Deployment:aiops-verification/verification-api",
            "approval_status": "awaiting_approval", "change_summary": {},
        }
        return _browser("r03_prepare", prepared=prepared, mutations=[{
            "request_id": "request-prepare", "method": "POST",
            "path": "/api/v1/incidents/incident-run-one/change-requests",
            "status": 201, "response_request_id": "request-prepare",
            "identities": {
                "change_request.id": "change-probe",
                "change_request.active_revision.id": "revision-probe",
            },
        }])

    def verify_r03_admin_denial(self, **_kwargs) -> BrowserResult:
        denial = {
            "request_id": "request-read", "status": 409,
            "response_request_id": "request-read",
            "error_code": "cluster_not_ready",
        }
        return _browser("r03_admin", live_evidence=denial, mutations=[{
            "method": "POST", "path": "/api/v1/admin/connector-commands",
            "identities": {}, **denial,
        }])

    def verify_r03_sre_denials(self, **kwargs) -> BrowserResult:
        dry_run = {
            "request_id": "request-dry", "status": 409,
            "response_request_id": "request-dry",
            "error_code": "cluster_not_ready",
        }
        grant = {"request_id": "request-grant", "status": 409,
                 "response_request_id": "request-grant",
                 "error_code": "cluster_not_ready"}
        approval = {
            "request_id": "request-approval", "status": 201,
            "response_request_id": "request-approval",
        }
        root = f"/api/v1/change-requests/{kwargs['prepared']['change_request_id']}"
        return _browser(
            "r03_sre", denials={"dry_run": dry_run, "grant": grant},
            approval=approval, active_commands=0, grants_created=0,
            mutations=[
                {
                    "method": "POST",
                    "path": f"/api/v1/incidents/{kwargs['incident_id']}/change-requests",
                    "identities": {}, **dry_run,
                },
                {
                    "method": "POST", "path": f"{root}/phase-approval/approve",
                    "identities": {"approval.id": "approval-1", "revision_id": "revision-probe"},
                    **approval,
                },
                {
                    "method": "POST", "path": f"{root}/phase-execution/start",
                    "identities": {}, **grant,
                },
            ],
        )


class ConnectorPoll:
    def unavailable(self, _scope, *, request_id: str) -> dict[str, object]:
        return {"request_id": request_id, "status": 409, "error_code": "cluster_not_ready"}


class Loki:
    def unavailable(self, _scope, *, request_id: str) -> dict[str, object]:
        return {"status": "failed", "error_code": "backend_unavailable", "evidence_refs": []}

    def recovered(self, scope, *, request_id: str) -> dict[str, object]:
        return {
            "retained_log_refs_sha256": scope.recovery_log_refs_sha256,
            "retained_query": {"status": "succeeded", "matched": 1,
                               "evidence_ref": {"source": "loki", "ref_id": "old"}},
            "fresh_probe_request_id": request_id + "-fresh",
            "fresh_query": {"status": "succeeded", "matched": 1,
                            "evidence_ref": {"source": "loki", "ref_id": "new"}},
        }


def _probe(tmp_path: Path):
    user = Session()
    admin = Session(admin=True)
    scope = load_recovery_scope(recovery_ledger(tmp_path, "R03"))
    probe = GatewayDependencyProbe(
        base_url="https://aiops.example", user=user, admin=admin,
        browser=Browser(), connector_poll=ConnectorPoll(), loki=Loki(),
        sre_username="sre", sre_password="sre-password",
        admin_username="admin", admin_password="admin-password",
    )
    return scope, probe, user


def test_gateway_dependency_probe_reads_exact_ready_owner_facts(tmp_path: Path) -> None:
    scope, probe, _user = _probe(tmp_path)

    connector = probe.snapshot_ready(scope, R03_TARGET)
    loki = probe.snapshot_ready(scope, R04_TARGET)

    assert connector["heartbeat"] == "online"
    assert connector["read_verification"] == "verified"
    assert loki == {
        "availability": "available", "reason_code": "ready",
        "observability_readiness": "ready", "loki_state": "available",
    }


def test_gateway_dependency_probe_normalizes_all_connector_denials(tmp_path: Path) -> None:
    scope, probe, user = _probe(tmp_path)
    prepared = probe.prepare_connector_probe(scope, operation_id="r03/execution/prepare")
    user.unavailable = {"connector"}

    result = probe.verify_connector_unavailable(
        scope, prepared, operation_id="r03/execution/deny",
    )

    assert set(result["denials"]) == {"live_evidence", "dry_run", "grant", "dispatch"}
    assert {item["error_code"] for item in result["denials"].values()} == {  # type: ignore[union-attr]
        "cluster_not_ready"
    }
    assert result["active_commands"] == result["grants_created"] == 0


def test_gateway_dependency_probe_does_not_wrap_loki_unavailability_as_evidence(
    tmp_path: Path,
) -> None:
    scope, probe, user = _probe(tmp_path)
    user.unavailable = {"observability"}

    unavailable = probe.verify_loki_unavailable(
        scope, operation_id="r04/execution/unavailable",
    )
    user.unavailable = set()
    recovered = probe.verify_loki_recovered(
        scope, operation_id="r04/execution/recovered",
    )

    assert unavailable["mcp"]["evidence_refs"] == []  # type: ignore[index]
    assert recovered["fresh_query"]["evidence_ref"]["source"] == "loki"  # type: ignore[index]


def test_gateway_dependency_probe_rejects_parallel_owner_outage(tmp_path: Path) -> None:
    scope, probe, user = _probe(tmp_path)
    user.unavailable = {"connector", "observability"}

    with pytest.raises(ValueError, match="publicly ready"):
        probe.verify_connector_unavailable(
            scope,
            {
                "change_request_id": "change-probe", "phase_id": "phase-probe",
                "revision_id": "revision-probe",
            },
            operation_id="r03/execution/deny",
        )


def test_gateway_dependency_probe_rejects_wrong_cluster_identity(tmp_path: Path) -> None:
    scope, probe, _user = _probe(tmp_path)

    with pytest.raises(ValueError, match="not unique"):
        probe.snapshot_ready(replace(scope, cluster_id="other-cluster"), R03_TARGET)


def test_gateway_dependency_probe_rejects_unbound_denial_identity(tmp_path: Path) -> None:
    scope, probe, user = _probe(tmp_path)
    user.unavailable = {"connector"}
    browser = Browser()
    original = browser.verify_r03_admin_denial

    def mismatched(**kwargs) -> BrowserResult:
        result = original(**kwargs)
        result.summary["mutations"][0]["request_id"] = "different-request"  # type: ignore[index]
        return result

    browser.verify_r03_admin_denial = mismatched  # type: ignore[method-assign]
    probe.browser = browser

    with pytest.raises(ValueError, match="not reconciled"):
        probe.verify_connector_unavailable(
            scope,
            {
                "change_request_id": "change-probe", "phase_id": "phase-probe",
                "revision_id": "revision-probe",
            },
            operation_id="r03/execution/deny",
        )


def test_connector_poll_probe_uses_credential_only_in_authorization_header(
    tmp_path: Path,
) -> None:
    scope = load_recovery_scope(recovery_ledger(tmp_path, "R03"))
    probe = ConnectorPollGatewayProbe(
        "https://aiops.example", CredentialValue("connector-credential"),
    )

    class Response:
        status = 409

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def read() -> bytes:
            return b'{"request_id":"r03/dispatch","error":{"code":"cluster_not_ready"}}'

    requests = []

    class Opener:
        def open(self, request, **_kwargs):
            requests.append(request)
            return Response()

    probe.opener = Opener()
    result = probe.unavailable(scope, request_id="r03/dispatch")

    assert result == {
        "request_id": "r03/dispatch", "status": 409, "error_code": "cluster_not_ready",
    }
    assert requests[0].get_header("Authorization") == "Bearer connector-credential"
    assert "connector-credential" not in str(result)
