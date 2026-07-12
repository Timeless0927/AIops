"""T24 legacy contract retirement acceptance."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from apps.aiops_k8s_gateway import main as gateway_main


LEGACY_MODULES = {
    "action_control_service.py",
    "agent_run_service.py",
    "approval_execution_service.py",
    "approval_service.py",
    "audit_chain_service.py",
    "case_profile_service.py",
    "command_service.py",
    "connector_router.py",
    "diagnosis_writeback.py",
    "evidence_service.py",
    "report_service.py",
    "runbook_service.py",
    "settings_service.py",
}


def _status(url: str, *, method: str = "GET") -> int:
    request = urllib.request.Request(
        url,
        data=b"{}" if method == "POST" else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_gateway_exposes_no_legacy_product_or_static_asset_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))
    gateway_main._SESSIONS.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        routes = {
            "/": "GET",
            "/index.html": "GET",
            "/api/incidents/active": "GET",
            "/api/agent-runs": "GET",
            "/api/approval-requests": "GET",
            "/api/incidents/incident-1/workbench": "GET",
            "/incidents/incident-1": "GET",
            "/connectors/register": "POST",
            "/notifications/send": "POST",
        }
        assert {_status(base_url + path, method=method) for path, method in routes.items()} == {404}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_repository_has_one_versioned_console_contract() -> None:
    gateway = Path("apps/aiops_k8s_gateway")
    assert not LEGACY_MODULES & {path.name for path in gateway.glob("*.py")}
    source = (gateway / "main.py").read_text(encoding="utf-8")
    assert "StaticFiles" not in source
    assert "incident_store" not in source
    assert "audit_log" not in source

    specification = json.loads(Path("api/openapi/gateway-v1.json").read_text(encoding="utf-8"))
    assert all(path.startswith(("/auth/", "/api/v1/")) for path in specification["paths"])
