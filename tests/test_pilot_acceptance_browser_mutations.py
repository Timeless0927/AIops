from __future__ import annotations

import json
import urllib.request
from itertools import count
from pathlib import Path

import pytest

from aiops.acceptance.browser_mutations import (
    BrowserMutationBinding,
    BrowserMutationError,
    reconcile_unique_browser_operation,
)
from aiops.acceptance.gate_contract import GATE_CONTRACT_REVISION
from aiops.acceptance.ledger import AcceptanceLedger
from tests.pilot_acceptance_support import create_evidence


def _ledger(tmp_path: Path) -> AcceptanceLedger:
    ids = count(1)
    return create_evidence(
        tmp_path / "acceptance",
        acceptance_id="v0.1.0-browser-reconcile",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        acceptance_tool_sha256="b" * 64,
        gate_contract_revision=GATE_CONTRACT_REVISION,
        kube_context="pilot-clean",
        cluster_identity_sha256="c" * 64,
        access_profile="http_nodeport",
        now=lambda: "2026-07-16T01:02:03Z",
        new_execution_id=lambda: f"execution-{next(ids)}",
    )


def test_browser_resume_requires_one_actor_scoped_public_fact(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path)
    evidence.start_gate("P01")
    evidence.bind_operation("P01", kind="console_mutation", operation_id="req-1")

    fact = reconcile_unique_browser_operation(
        evidence,
        "P01",
        "req-1",
        lambda request_id: [{"request_id": request_id, "object_id": "user-1", "revision": 1}],
    )

    assert fact == {"request_id": "req-1", "object_id": "user-1", "revision": 1}
    assert evidence.resume_gate("P01").reconciliations[0]["outcome"] == "succeeded"


@pytest.mark.parametrize("matches", [[], [
    {"request_id": "req-1", "object_id": "user-1", "revision": 1},
    {"request_id": "req-1", "object_id": "user-2", "revision": 2},
]])
def test_browser_resume_fails_closed_for_zero_or_multiple_matches(
    tmp_path: Path, matches: list[dict[str, object]],
) -> None:
    evidence = _ledger(tmp_path)
    evidence.start_gate("P01")
    evidence.bind_operation("P01", kind="console_mutation", operation_id="req-1")

    with pytest.raises(BrowserMutationError, match="exactly one"):
        reconcile_unique_browser_operation(
            evidence, "P01", "req-1", lambda _request_id: matches,
        )

    reconciliation = evidence.resume_gate("P01").reconciliations[0]
    assert reconciliation["outcome"] == "unprovable"
    assert reconciliation["public_fact"]["match_count"] == len(matches)


def test_browser_resume_rejects_mismatched_or_secret_bearing_fact(tmp_path: Path) -> None:
    for index, fact in enumerate((
        {"request_id": "wrong", "object_id": "user-1", "revision": 1},
        {"request_id": "req-1", "session_token": "must-not-persist"},
    )):
        evidence = _ledger(tmp_path / str(index))
        evidence.start_gate("P01")
        evidence.bind_operation("P01", kind="console_mutation", operation_id="req-1")

        with pytest.raises(BrowserMutationError):
            reconcile_unique_browser_operation(
                evidence, "P01", "req-1", lambda _request_id, item=fact: [item],
            )

        reconciliation = evidence.resume_gate("P01").reconciliations[0]
        assert reconciliation["outcome"] == "unprovable"
        assert "must-not-persist" not in str(reconciliation)


def test_browser_mutation_http_failure_is_not_a_succeeded_operation(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path)
    evidence.start_gate("P01")
    with BrowserMutationBinding(evidence, "P01") as binding:
        headers = {
            "Authorization": f"Bearer {binding.callback['token']}",
            "Content-Type": "application/json",
        }
        for endpoint, payload in (
            ("intent", {"request_id": "req-1", "method": "POST", "path": "/api/v1/admin/users"}),
            ("result", {
                "request_id": "req-1", "status": 404, "response_request_id": "req-1",
                "identities": {}, "error_code": "not_found",
            }),
        ):
            request = urllib.request.Request(
                f"{binding.callback['url']}/{endpoint}",
                data=json.dumps(payload).encode(), headers=headers, method="POST",
            )
            with urllib.request.urlopen(request) as response:
                assert response.status == 204

    reconciliation = evidence.resume_gate("P01").reconciliations[0]
    assert reconciliation["outcome"] == "failed"
    assert reconciliation["public_fact"]["error_code"] == "not_found"


def test_browser_mutation_accepts_distinct_object_and_revision_identities(
    tmp_path: Path,
) -> None:
    evidence = _ledger(tmp_path)
    evidence.start_gate("P01")
    with BrowserMutationBinding(evidence, "P01") as binding:
        headers = {
            "Authorization": f"Bearer {binding.callback['token']}",
            "Content-Type": "application/json",
        }
        for endpoint, payload in (
            ("intent", {
                "request_id": "req-approval", "method": "POST",
                "path": "/api/v1/change-requests/change-1/phase-approval/approve",
            }),
            ("result", {
                "request_id": "req-approval", "status": 201,
                "response_request_id": "req-approval",
                "identities": {
                    "phase_review.approval.id": "approval-1",
                    "phase_review.revision_id": "revision-1",
                },
            }),
        ):
            request = urllib.request.Request(
                f"{binding.callback['url']}/{endpoint}",
                data=json.dumps(payload).encode(), headers=headers, method="POST",
            )
            with urllib.request.urlopen(request) as response:
                assert response.status == 204

    assert evidence.resume_gate("P01").reconciliations[0]["outcome"] == "succeeded"


def test_browser_mutation_accepts_product_updated_at_as_revision_fact(
    tmp_path: Path,
) -> None:
    evidence = _ledger(tmp_path)
    evidence.start_gate("P01")
    with BrowserMutationBinding(evidence, "P01") as binding:
        headers = {
            "Authorization": f"Bearer {binding.callback['token']}",
            "Content-Type": "application/json",
        }
        for endpoint, payload in (
            ("intent", {
                "request_id": "req-cluster", "method": "PATCH",
                "path": "/api/v1/admin/clusters/pilot-cluster",
            }),
            ("result", {
                "request_id": "req-cluster", "status": 200,
                "response_request_id": "req-cluster",
                "identities": {
                    "cluster.cluster_id": "pilot-cluster",
                    "cluster.updated_at": "1752853800.0",
                },
            }),
        ):
            request = urllib.request.Request(
                f"{binding.callback['url']}/{endpoint}",
                data=json.dumps(payload).encode(), headers=headers, method="POST",
            )
            with urllib.request.urlopen(request) as response:
                assert response.status == 204

    assert evidence.resume_gate("P01").reconciliations[0]["outcome"] == "succeeded"
