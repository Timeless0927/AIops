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


def test_incident_detail_static_assets_exist_and_are_self_contained() -> None:
    html = (CONSOLE / "static" / "incident-detail.html").read_text(encoding="utf-8")
    js = (CONSOLE / "static" / "incident-detail.js").read_text(encoding="utf-8")
    css = (CONSOLE / "static" / "incident-detail.css").read_text(encoding="utf-8")

    assert "../fixtures/incident-detail-fixtures.js" in html
    assert "./incident-detail.js" in html
    assert "fetch(" not in js
    assert "XMLHttpRequest" not in js
    assert "execute" not in html.lower()
    assert "mutation" not in html.lower()
    assert 'id="access-list"' in html
    assert 'id="access-blocked-reason"' in html
    assert "can_view_raw_evidence" in js
    assert "can_view_cost" in js
    assert "can_approve" in js
    assert "blocked_reason" in js
    assert ".evidence-grid" in css
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
    assert "GET /api/incidents/{incident_id}" in readme
    assert "GET /incidents/{incident_id}" in readme
    assert "never calls Hermes, Connector, MCP, Prometheus, Loki, or Feishu" in readme
    assert "Full chain-of-thought is never shown" in readme
