"""Static AIOps Console incident detail slice contract tests."""

from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONSOLE = ROOT / "apps" / "aiops_console"


def _fixtures() -> dict[str, object]:
    source = (CONSOLE / "fixtures" / "incident-detail-fixtures.js").read_text(encoding="utf-8")
    match = re.search(r"window\.AIOPS_INCIDENT_FIXTURES\s*=\s*(\{.*\});\s*$", source, re.S)
    assert match, "fixture script must assign a JSON-compatible fixture object"
    data = json.loads(match.group(1))
    assert isinstance(data, dict)
    return data


def _overview_fixtures() -> dict[str, object]:
    source = (CONSOLE / "fixtures" / "console-overview-fixtures.js").read_text(encoding="utf-8")
    match = re.search(r"window\.AIOPS_CONSOLE_OVERVIEW_FIXTURES\s*=\s*(\{.*\});\s*$", source, re.S)
    assert match, "overview fixture script must assign a JSON-compatible fixture object"
    data = json.loads(match.group(1))
    assert isinstance(data, dict)
    return data


def test_incident_detail_static_assets_exist_and_are_self_contained() -> None:
    html = (CONSOLE / "static" / "incident-detail.html").read_text(encoding="utf-8")
    js = (CONSOLE / "static" / "incident-detail.js").read_text(encoding="utf-8")
    shell_js = (CONSOLE / "static" / "console-shell.js").read_text(encoding="utf-8")
    css = (CONSOLE / "static" / "incident-detail.css").read_text(encoding="utf-8")
    shell_css = (CONSOLE / "static" / "console-shell.css").read_text(encoding="utf-8")

    assert "../fixtures/incident-detail-fixtures.js" in html
    assert "./console-shell.css" in html
    assert "./console-shell.js" in html
    assert "./incident-detail.js" in html
    assert "fetch(" not in shell_js
    assert "XMLHttpRequest" not in js
    assert "XMLHttpRequest" not in shell_js
    assert "fetch(gatewayProcessUrl(incidentId)" in js
    assert "aiopsGatewayBaseUrl" in js
    assert '"Authorization": `Bearer ${token()}`' in js
    assert "/api/incidents/" in js
    assert "diagnosis-process" in js
    assert "canLoadGatewayProcess" in js
    for forbidden in ["hermes", "connector", "mcp", "prometheus", "loki", "feishu"]:
        assert f"/{forbidden}" not in js.lower()
        assert f"{forbidden}://" not in js.lower()
    assert "execute" not in html.lower()
    assert "mutation" not in html.lower()
    assert 'id="access-list"' in html
    assert 'id="access-blocked-reason"' in html
    assert "can_view_raw_evidence" in js
    assert "can_view_cost" in js
    assert "can_approve" in js
    assert "blocked_reason" in js
    assert ".evidence-grid" in css
    assert ".missing-list" in css
    assert ".markdown-text" in css
    assert ".console-app" in shell_css
    assert ".console-nav" in shell_css
    assert ".access-list" in css
    assert "@media" in css


def test_incident_detail_fixtures_cover_required_page_states() -> None:
    fixtures = _fixtures()

    assert set(fixtures) == {"complete", "empty", "partial", "failed"}
    assert fixtures["empty"]["diagnosis"] is None
    assert fixtures["empty"]["evidence"] == []
    assert fixtures["partial"]["diagnosis"]["status"] == "partial"
    assert fixtures["failed"]["diagnosis"]["status"] == "failed"

    partial_statuses = {item["status"] for item in fixtures["partial"]["evidence"]}
    failed_statuses = {item["status"] for item in fixtures["failed"]["evidence"]}
    assert {"succeeded", "failed", "empty", "partial"} <= partial_statuses
    assert {"failed", "skipped"} <= failed_statuses


def test_incident_detail_evidence_and_actions_are_read_only() -> None:
    fixtures = _fixtures()
    expected_kinds = {"prometheus", "loki", "k8s", "topology"}

    for scenario in fixtures.values():
        evidence = scenario["evidence"]
        if evidence:
            assert expected_kinds == {item["kind"] for item in evidence}
        for action in scenario["actions"]:
            assert action["execution_enabled"] is False


def test_incident_detail_permission_limits_are_fixture_backed_and_rendered() -> None:
    fixtures = _fixtures()
    js = (CONSOLE / "static" / "incident-detail.js").read_text(encoding="utf-8")

    assert "renderAccess(incident.permissions || {})" in js
    for scenario_name, scenario in fixtures.items():
        permissions = scenario["incident"]["permissions"]
        assert set(permissions) >= {"can_view_raw_evidence", "can_view_cost", "can_approve", "blocked_reason"}
        assert permissions["can_view_raw_evidence"] is False
        assert permissions["can_approve"] is False
        assert isinstance(permissions["blocked_reason"], str)
        assert permissions["blocked_reason"]
        if scenario_name == "partial":
            assert permissions["can_view_cost"] is False


def test_incident_detail_documents_gateway_only_api_assumptions() -> None:
    readme = (CONSOLE / "README.md").read_text(encoding="utf-8")

    assert "Gateway only" in readme or "Gateway-only" in readme
    assert "GET /api/incidents/{incident_id}/diagnosis-process" in readme
    assert "GET /incidents/{incident_id}" in readme
    assert "never calls Hermes, Connector, MCP, Prometheus, Loki, or Feishu" in readme
    assert "Full chain-of-thought is never shown" in readme


def test_console_overview_static_assets_are_gateway_safe() -> None:
    html = (CONSOLE / "static" / "console-overview.html").read_text(encoding="utf-8")
    shell_js = (CONSOLE / "static" / "console-shell.js").read_text(encoding="utf-8")
    overview_js = (CONSOLE / "static" / "console-overview.js").read_text(encoding="utf-8")
    css = (CONSOLE / "static" / "console-overview.css").read_text(encoding="utf-8")
    fixture = (CONSOLE / "fixtures" / "console-overview-fixtures.js").read_text(encoding="utf-8")

    assert "../fixtures/console-overview-fixtures.js" in html
    assert "./console-shell.css" in html
    assert "./console-overview.css" in html
    assert "./console-shell.js" in html
    assert "./console-overview.js" in html
    assert html.index("./console-overview.js") < html.index("./console-shell.js")
    assert "fetch(" not in shell_js
    assert "XMLHttpRequest" not in shell_js
    assert "XMLHttpRequest" not in overview_js
    assert 'fetch(apiUrl("/api/incidents/active")' in overview_js
    assert 'fetch(apiUrl("/auth/login")' in overview_js
    assert 'id="gatewayBaseUrl"' in html
    assert "aiopsGatewayBaseUrl" in overview_js
    for forbidden in ["hermes", "connector", "mcp", "prometheus", "loki", "feishu"]:
        assert f"/{forbidden}" not in overview_js.lower()
        assert f"{forbidden}://" not in overview_js.lower()
    assert "innerHTML" not in overview_js
    assert "execute" not in html.lower()
    assert "mutation" not in html.lower()
    assert "planned" in html
    assert "unavailable" in html
    assert "Full chain-of-thought" not in html
    assert "window.AIOPS_CONSOLE_OVERVIEW_FIXTURES" in fixture
    assert "window.AIOPS_CONSOLE_OVERVIEW_FIXTURES" in overview_js
    assert "sessionStorage" in overview_js
    assert ".overview-grid" in css


def test_console_overview_shows_complete_skeleton_and_agent_process() -> None:
    html = (CONSOLE / "static" / "console-overview.html").read_text(encoding="utf-8")
    fixture = _overview_fixtures()
    overview_js = (CONSOLE / "static" / "console-overview.js").read_text(encoding="utf-8")

    for label in [
        "总览",
        "事件",
        "诊断",
        "审批",
        "通知",
        "执行",
        "审计",
        "设置",
    ]:
        assert label in html
    assert 'aria-disabled="true"' in html
    assert "工具调用与证据" in html
    tools = fixture["summary"]["tools"]
    incidents = fixture["summary"]["incidents"]
    notifications = fixture["summary"]["notifications"]
    assert {tool["name"] for tool in tools} >= {"query.prometheus", "logs.cluster_search"}
    assert any("approval" in incident["tags"] for incident in incidents)
    assert {item["delivery_status"] for item in notifications} >= {"sent", "failed"}
    assert "renderNotifications(summary.notifications || [])" in overview_js
    assert "renderTools(summary.tools || [])" in overview_js
    assert "loadLiveIncidents" in overview_js
    assert "审批预览" in html
    assert "policy_requires_ic" in html
    assert "诊断成本" in html
    assert "Grafana 兜底" in html
