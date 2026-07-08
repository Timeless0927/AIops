"""Console Next incident report and feedback route tests."""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from apps.aiops_k8s_gateway import agent_run_service
from apps.aiops_k8s_gateway import action_control_service
from apps.aiops_k8s_gateway import approval_execution_service
from apps.aiops_k8s_gateway import approval_service
from apps.aiops_k8s_gateway import main as gateway_main
from apps.aiops_k8s_gateway import report_service
from toolsets.incident_store import IncidentStore


def _write_identity_config(path: Path) -> None:
    path.write_text(
        f"""
identity:
  store_path: "{path.with_name("identity.db")}"
  ldap:
    enabled: false
  users:
    - username: operator
      password: operator-pass
      display_name: Operator
      roles: [operator]
      scope:
        clusters: ["prod-a"]
        services: ["checkout"]
        teams: ["payments"]
        namespaces: ["default"]
    - username: outsider
      password: outsider-pass
      display_name: Outsider
      roles: [operator]
      scope:
        clusters: ["prod-b"]
        services: ["billing"]
        teams: ["finance"]
        namespaces: ["default"]
    - username: viewer
      password: viewer-pass
      display_name: Viewer
      roles: [viewer]
      scope:
        clusters: ["prod-a"]
        services: ["checkout"]
        teams: ["payments"]
        namespaces: ["default"]
    - username: owner
      password: owner-pass
      display_name: Service Owner
      roles: [operator]
      scope:
        clusters: ["prod-a"]
        services: ["checkout"]
        teams: ["payments"]
        namespaces: ["default"]
    - username: admin
      password: admin-pass
      display_name: Admin
      roles: [admin]
      scope:
        clusters: ["*"]
        services: ["*"]
        teams: ["*"]
        namespaces: ["*"]
""",
        encoding="utf-8",
    )


def _request_json(url: str, *, body: dict | None = None, token: str | None = None, method: str = "GET") -> tuple[int, dict]:
    data = json.dumps(body or {}).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def _request_text(url: str, *, token: str | None = None) -> tuple[int, str, str]:
    headers = {"Accept": "text/html"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read().decode("utf-8")


def _login(base_url: str, username: str, password: str) -> str:
    status, payload = _request_json(f"{base_url}/auth/login", body={"username": username, "password": password}, method="POST")
    assert status == 200
    return str(payload["token"])


@pytest.fixture
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config_path = tmp_path / "identity.yaml"
    data_dir = tmp_path / "data"
    _write_identity_config(config_path)
    monkeypatch.setenv("AIOPS_IDENTITY_CONFIG", str(config_path))
    monkeypatch.setenv("AIOPS_DATA_DIR", str(data_dir))

    old_report_db = report_service._DB
    old_run_db = agent_run_service._DB
    old_action_db = action_control_service._DB
    old_approval_db = approval_service._DB
    old_execution_db = approval_execution_service._DB
    old_audit_db = gateway_main.audit_log._DB
    old_incident_store = gateway_main.incident_store._STORE
    report_service._DB = report_service.ReportDB(data_dir / "reports_feedback.db")
    agent_run_service._DB = agent_run_service.AgentRunDB(data_dir / "agent_runs.db")
    action_control_service._DB = action_control_service.ActionControlDB(data_dir / "actions.db")
    approval_service._DB = approval_service.ApprovalRequestDB(data_dir / "approval_requests.db")
    approval_execution_service._DB = approval_execution_service.ApprovalExecutionDB(data_dir / "approval_executions.db")
    gateway_main.audit_log._DB = gateway_main.audit_log.AuditLogDB(data_dir / "audit_log.db")
    gateway_main.incident_store._STORE = IncidentStore(data_dir / "incidents.db")
    gateway_main._SESSIONS.clear()
    gateway_main._ROUTES.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        gateway_main._SESSIONS.clear()
        gateway_main._ROUTES.clear()
        report_service._DB.close()
        agent_run_service._DB.close()
        action_control_service._DB.close()
        approval_service._DB.close()
        approval_execution_service._DB.close()
        gateway_main.audit_log._DB.close()
        gateway_main.incident_store._STORE.close()
        report_service._DB = old_report_db
        agent_run_service._DB = old_run_db
        action_control_service._DB = old_action_db
        approval_service._DB = old_approval_db
        approval_execution_service._DB = old_execution_db
        gateway_main.audit_log._DB = old_audit_db
        gateway_main.incident_store._STORE = old_incident_store


def test_report_draft_publish_version_export_feedback_and_auth(gateway: str) -> None:
    incident_id = asyncio.run(
        gateway_main.incident_store.create_incident(
            "CheckoutErrors",
            "default",
            "prod-a",
            "checkout error rate high",
            service="checkout",
            team="payments",
        )
    )
    run = asyncio.run(
        agent_run_service.create_run(
            {
                "title": "checkout run",
                "message": "investigate checkout",
                "incident_id": incident_id,
                "scope": {
                    "cluster": "prod-a",
                    "namespace": "default",
                    "service": "checkout",
                    "team": "payments",
                },
            },
            actor_id="operator",
        )
    )
    run_id = str(run["run"]["run_id"])
    operator = _login(gateway, "operator", "operator-pass")
    outsider = _login(gateway, "outsider", "outsider-pass")
    viewer = _login(gateway, "viewer", "viewer-pass")

    public_status, public_payload = _request_json(f"{gateway}/api/incidents/{incident_id}/report")
    hidden_status, hidden_payload = _request_json(f"{gateway}/api/incidents/{incident_id}/report", token=outsider)
    viewer_draft_status, viewer_draft = _request_json(f"{gateway}/api/incidents/{incident_id}/report/draft", body={}, token=viewer, method="POST")
    draft_status, draft = _request_json(f"{gateway}/api/incidents/{incident_id}/report/draft", body={}, token=operator, method="POST")
    version_id = draft["report_version"]["version_id"]
    get_status, report = _request_json(f"{gateway}/api/incidents/{incident_id}/report", token=operator)
    html_status, content_type, html = _request_text(f"{gateway}/api/incidents/{incident_id}/report?format=html", token=operator)
    markdown_status, markdown_content_type, markdown = _request_text(f"{gateway}/api/incidents/{incident_id}/report?format=markdown", token=operator)
    publish_status, published = _request_json(
        f"{gateway}/api/incidents/{incident_id}/report/publish",
        body={"version_id": version_id},
        token=operator,
        method="POST",
    )
    republish_status, republish = _request_json(
        f"{gateway}/api/incidents/{incident_id}/report/publish",
        body={"version_id": version_id},
        token=operator,
        method="POST",
    )
    edit_status, edited = _request_json(
        f"{gateway}/api/incidents/{incident_id}/report/draft",
        body={"html": "<article><h1>edited</h1><script>alert(1)</script></article>"},
        token=operator,
        method="POST",
    )
    feedback_payloads = [
        {"target_type": "diagnosis", "target_id": incident_id, "incident_id": incident_id, "rating": "neutral", "comment": "root cause unknown"},
        {"target_type": "evidence", "target_id": "ev-1", "incident_id": incident_id, "rating": "negative", "comment": "missing logs"},
        {"target_type": "action_proposal", "target_id": "proposal-1", "incident_id": incident_id, "rating": "positive", "comment": "safe action"},
        {"target_type": "report", "target_id": version_id, "incident_id": incident_id, "rating": "positive", "comment": "ready"},
        {"target_type": "report", "target_id": version_id, "run_id": run_id, "rating": "positive", "comment": "visible on run"},
    ]
    feedback_results = [
        _request_json(f"{gateway}/api/feedback", body=payload, token=operator, method="POST")
        for payload in feedback_payloads
    ]
    final_status, final_report = _request_json(f"{gateway}/api/incidents/{incident_id}/report", token=operator)
    run_feedback_status, run_feedback = _request_json(f"{gateway}/api/agent-runs/{run_id}/feedback", token=operator)

    assert public_status == 401
    assert public_payload["error"]["code"] == "unauthorized"
    assert hidden_status == 403
    assert hidden_payload["error"]["code"] == "forbidden"
    assert viewer_draft_status == 403
    assert viewer_draft["error"]["code"] == "forbidden"
    assert draft_status == 201
    assert draft["report_version"]["status"] == "draft"
    assert "root_cause" in draft["report_version"]["unknowns"]
    assert "unknown" in draft["report_version"]["html"]
    assert get_status == 200
    assert report["report"]["latest_report"]["version_id"] == version_id
    assert html_status == 200
    assert "text/html" in content_type
    assert "CheckoutErrors" in html
    assert markdown_status == 200
    assert "text/markdown" in markdown_content_type
    assert "## Responsibility Chain" in markdown
    assert publish_status == 200
    assert published["report_version"]["status"] == "published"
    assert republish_status == 409
    assert republish["error"]["code"] == "immutable_report"
    assert edit_status == 201
    assert edited["report_version"]["version_number"] == 2
    assert "<script>" not in edited["report_version"]["html"]
    assert all(status == 201 for status, _ in feedback_results)
    assert final_status == 200
    assert {item["target_type"] for item in final_report["report"]["feedback"]} >= {
        "diagnosis",
        "evidence",
        "action_proposal",
        "report",
    }
    assert run_feedback_status == 200
    assert run_feedback["feedback"][0]["run_id"] == run_id


def test_kb_candidates_require_allowed_sources_and_stay_candidates(gateway: str) -> None:
    incident_id = asyncio.run(
        gateway_main.incident_store.create_incident(
            "CheckoutLatency",
            "default",
            "prod-a",
            "checkout latency high",
            service="checkout",
            team="payments",
        )
    )
    asyncio.run(
        gateway_main.incident_store.add_evidence(
            incident_id,
            "logs",
            "oo-log-1",
            "checkout timeout spike",
            payload={"root_cause": "downstream payment timeout"},
        )
    )
    operator = _login(gateway, "operator", "operator-pass")
    draft_status, draft = _request_json(f"{gateway}/api/incidents/{incident_id}/report/draft", body={}, token=operator, method="POST")
    assert draft_status == 201
    version_id = draft["report_version"]["version_id"]
    publish_status, _ = _request_json(
        f"{gateway}/api/incidents/{incident_id}/report/publish",
        body={"version_id": version_id},
        token=operator,
        method="POST",
    )
    unresolved_status, unresolved = _request_json(
        f"{gateway}/api/incidents/{incident_id}/report/kb-candidates",
        body={"sources": ["resolved_incident"]},
        token=operator,
        method="POST",
    )
    report_status, report_candidates = _request_json(
        f"{gateway}/api/incidents/{incident_id}/report/kb-candidates",
        body={"sources": ["published_report"]},
        token=operator,
        method="POST",
    )
    wrong_feedback_status, _ = _request_json(
        f"{gateway}/api/feedback",
        body={"target_type": "diagnosis", "target_id": incident_id, "incident_id": incident_id, "rating": "wrong", "comment": "not it"},
        token=operator,
        method="POST",
    )
    correct_feedback_status, _ = _request_json(
        f"{gateway}/api/feedback",
        body={"target_type": "diagnosis", "target_id": incident_id, "incident_id": incident_id, "rating": "correct", "comment": "payment timeout confirmed"},
        token=operator,
        method="POST",
    )
    metadata_only_status, _ = _request_json(
        f"{gateway}/api/feedback",
        body={
            "target_type": "diagnosis",
            "target_id": "metadata-only",
            "incident_id": incident_id,
            "rating": "neutral",
            "comment": "metadata should not mark this correct",
            "metadata": {"correct": True},
        },
        token=operator,
        method="POST",
    )
    feedback_status, feedback_candidates = _request_json(
        f"{gateway}/api/incidents/{incident_id}/report/kb-candidates",
        body={"sources": ["correct_feedback"]},
        token=operator,
        method="POST",
    )
    viewer_kb_status, viewer_kb = _request_json(
        f"{gateway}/api/incidents/{incident_id}/report/kb-candidates",
        body={"sources": ["published_report"]},
        token=_login(gateway, "viewer", "viewer-pass"),
        method="POST",
    )

    approval, _ = approval_service.create_request(
        {
            "incident_id": incident_id,
            "session_id": "run-1",
            "action_proposal_id": "proposal-1",
            "risk_level": "medium",
            "requested_by": "operator",
            "reason": "restart checkout",
            "action_summary": "restart checkout deployment",
            "resource_scope": {"cluster": "prod-a", "namespace": "default", "service": "checkout", "team": "payments"},
            "rollback_plan": "rollback deployment",
            "evidence_refs": [{"ref_id": "oo-log-1"}],
        },
        actor_id="operator",
        request_id="req-approval",
    )
    approval, _ = approval_service.decide(
        str(approval["approval_id"]),
        decision=approval_service.APPROVED,
        actor_id="operator",
        reason="approved",
        request_id="req-decision",
    )
    execution, _ = approval_execution_service.create_or_replay(
        approval,
        {
            "idempotency_key": "exec-1",
            "cluster_id": "prod-a",
            "namespace": "default",
            "argv": ["kubectl", "rollout", "restart", "deployment/checkout"],
            "preflight_argv": ["kubectl", "get", "deployment/checkout"],
            "post_check_argv": ["kubectl", "rollout", "status", "deployment/checkout"],
        },
        actor_id="gateway",
    )
    approval_execution_service.update_execution(
        str(approval["approval_id"]),
        "succeeded",
        post_check_result={"status": "succeeded"},
    )
    action_status, action_candidates = _request_json(
        f"{gateway}/api/incidents/{incident_id}/report/kb-candidates",
        body={"sources": ["successful_action_post_check"]},
        token=operator,
        method="POST",
    )
    asyncio.run(gateway_main.incident_store.update_status(incident_id, "resolved"))
    resolved_status, resolved_candidates = _request_json(
        f"{gateway}/api/incidents/{incident_id}/report/kb-candidates",
        body={"sources": ["resolved_incident"]},
        token=operator,
        method="POST",
    )
    snapshot_status, snapshot = _request_json(f"{gateway}/api/incidents/{incident_id}/report", token=operator)

    assert publish_status == 200
    assert unresolved_status == 201
    assert unresolved["kb_candidates"] == []
    assert report_status == 201
    assert report_candidates["kb_candidates"][0]["source_type"] == "published_report"
    assert wrong_feedback_status == 201
    assert correct_feedback_status == 201
    assert metadata_only_status == 201
    assert feedback_status == 201
    assert len(feedback_candidates["kb_candidates"]) == 1
    assert feedback_candidates["kb_candidates"][0]["known_root_cause"] == "payment timeout confirmed"
    assert viewer_kb_status == 403
    assert viewer_kb["error"]["code"] == "forbidden"
    assert action_status == 201
    assert action_candidates["kb_candidates"][0]["metadata"]["execution_id"] == execution["execution_id"]
    assert resolved_status == 201
    assert resolved_candidates["kb_candidates"][0]["source_type"] == "resolved_incident"
    assert snapshot_status == 200
    assert {item["status"] for item in snapshot["report"]["kb_candidates"]} == {"candidate"}
    assert all(item["updated_at"] for item in snapshot["report"]["kb_candidates"])
    assert {item["source_type"] for item in snapshot["report"]["kb_candidates"]} >= {
        "published_report",
        "correct_feedback",
        "successful_action_post_check",
        "resolved_incident",
    }


def test_inline_kb_candidate_approval_agent_context_and_policy_boundary(gateway: str) -> None:
    incident_id = asyncio.run(
        gateway_main.incident_store.create_incident(
            "CheckoutKB",
            "default",
            "prod-a",
            "checkout errors repeat",
            service="checkout",
            team="payments",
        )
    )
    asyncio.run(
        gateway_main.incident_store.add_evidence(
            incident_id,
            "logs",
            "oo-log-kb",
            "checkout payment timeout",
            payload={"root_cause": "payment timeout"},
        )
    )
    operator = _login(gateway, "operator", "operator-pass")
    owner = _login(gateway, "owner", "owner-pass")
    viewer = _login(gateway, "viewer", "viewer-pass")
    admin = _login(gateway, "admin", "admin-pass")

    draft_status, draft = _request_json(f"{gateway}/api/incidents/{incident_id}/report/draft", body={}, token=operator, method="POST")
    assert draft_status == 201
    _request_json(
        f"{gateway}/api/incidents/{incident_id}/report/publish",
        body={"version_id": draft["report_version"]["version_id"]},
        token=operator,
        method="POST",
    )
    _request_json(
        f"{gateway}/api/feedback",
        body={"target_type": "diagnosis", "target_id": incident_id, "incident_id": incident_id, "rating": "correct", "comment": "payment timeout confirmed"},
        token=operator,
        method="POST",
    )
    report_status, report_candidates = _request_json(
        f"{gateway}/api/incidents/{incident_id}/report/kb-candidates",
        body={"sources": ["published_report", "correct_feedback"]},
        token=operator,
        method="POST",
    )
    candidates = report_candidates["kb_candidates"]
    viewer_approve_status, viewer_approve = _request_json(
        f"{gateway}/api/incidents/{incident_id}/report/kb-candidates/{candidates[0]['candidate_id']}/approve",
        body={},
        token=viewer,
        method="POST",
    )
    owner_approve_status, owner_approved = _request_json(
        f"{gateway}/api/incidents/{incident_id}/report/kb-candidates/{candidates[0]['candidate_id']}/approve",
        body={},
        token=owner,
        method="POST",
    )
    admin_approve_status, admin_approved = _request_json(
        f"{gateway}/api/incidents/{incident_id}/report/kb-candidates/{candidates[1]['candidate_id']}/approve",
        body={},
        token=admin,
        method="POST",
    )
    run_status, run_payload = _request_json(
        f"{gateway}/api/agent-runs",
        body={
            "title": "KB enriched run",
            "message": "investigate checkout",
            "incident_id": incident_id,
            "runbook_skeleton": "k8s_workload",
            "scope": {"cluster": "prod-a", "namespace": "default", "service": "checkout", "team": "payments"},
        },
        token=operator,
        method="POST",
    )
    propose_status, proposed = _request_json(
        f"{gateway}/api/actions/propose",
        body={
            "action_type": "restart_deployment",
            "cluster": "prod-a",
            "namespace": "default",
            "service": "checkout",
            "team": "payments",
            "deployment": "checkout",
            "incident_id": incident_id,
            "session_id": "sess-kb-policy",
            "reason": "restart checkout after error spike",
            "idempotency_key": "idem-kb-policy",
        },
        token=operator,
        method="POST",
    )
    audit_rows = asyncio.run(gateway_main.audit_log.query_audit(limit=100))

    assert report_status == 201
    assert len(candidates) == 2
    assert viewer_approve_status == 403
    assert viewer_approve["error"]["code"] == "forbidden"
    assert owner_approve_status == 200
    assert owner_approved["kb_candidate"]["status"] == "approved"
    assert owner_approved["kb_candidate"]["approved_by_admin_override"] is False
    assert admin_approve_status == 200
    assert admin_approved["kb_candidate"]["approved_by_admin_override"] is True
    assert any(row["what"] == "kb_candidate_approve" and row["result"] == "admin_override" for row in audit_rows)
    assert run_status == 201
    snapshot = run_payload["snapshot"]
    assert snapshot["run"]["runbook_skeleton"] == "k8s_workload"
    assert snapshot["knowledge_context"]
    assert snapshot["run"]["metadata"]["knowledge_context_count"] == 2
    assert any(ref.get("source") == "knowledge_base" for ref in snapshot["steps"][0]["evidence_refs"])
    assert any(event["event_type"] == "knowledge_context_retrieved" for event in snapshot["timeline"])
    assert propose_status == 201
    assert proposed["policy"]["decision"] == "approval_required"
    assert proposed["approval_request"]["status"] == "pending"
