"""C03 actor-scoped Incident Report Library HTTP contract."""

from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import jsonschema

from apps.aiops_k8s_gateway import main as gateway_main
from test_gateway_v1_report_contract import _bound_incident, _request, _resolve_incident


def _validate(spec: dict[str, object], payload: dict[str, object]) -> None:
    jsonschema.Draft202012Validator(
        spec["components"]["schemas"]["IncidentReportLibraryResponse"],  # type: ignore[index]
        resolver=jsonschema.RefResolver.from_schema(spec),
    ).validate(payload)


def test_report_library_is_scoped_summary_without_report_payload(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AIOPS_BOOTSTRAP_ADMIN_PASSWORD", "correct-horse-battery-staple")
    monkeypatch.delenv("AIOPS_IDENTITY_CONFIG", raising=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    spec = json.loads(Path("api/openapi/gateway-v1.json").read_text())

    try:
        denied_status, denied, _ = _request(f"{base_url}/api/v1/reports")
        assert denied_status == 401
        assert denied["error"]["code"] == "unauthorized"  # type: ignore[index]
        _, _, set_cookie = _request(
            f"{base_url}/auth/login",
            body={
                "username": "admin", "password": "correct-horse-battery-staple",
                "session_mode": "cookie",
            },
        )
        admin_cookie = set_cookie.split(";", 1)[0] if set_cookie else ""
        incident_id = _bound_incident()
        _resolve_incident(incident_id)

        status, listing, _ = _request(f"{base_url}/api/v1/reports", cookie=admin_cookie)
        assert status == 200
        assert listing["reports"][0]["incident"]["id"] == incident_id  # type: ignore[index]
        assert listing["reports"][0]["state"] == "draft"  # type: ignore[index]
        assert "narrative" not in json.dumps(listing)
        assert "decision_action_history" not in json.dumps(listing)
        _validate(spec, listing)

        _, outsider = gateway_main._identity_administration().mutate(
            collection="users", target_id=None,
            payload={
                "username": "outsider", "display_name": "Other SRE",
                "password": "safe-password",
            },
            actor_id="admin", reason="test", action="users_create",
            request_id="req-outsider",
        )
        _, other_team = gateway_main._identity_administration().mutate(
            collection="teams", target_id=None,
            payload={"name": "Other", "description": "Other team"},
            actor_id="admin", reason="test", action="teams_create",
            request_id="req-other-team",
        )
        for collection, payload, action in (
            (
                "team-memberships",
                {"user_id": outsider["id"], "team_id": other_team["id"]},
                "team-memberships_create",
            ),
            (
                "role-bindings",
                {
                    "user_id": outsider["id"], "role": "sre",
                    "scope_type": "team", "scope_id": other_team["id"],
                },
                "role-bindings_create",
            ),
        ):
            gateway_main._identity_administration().mutate(
                collection=collection, target_id=None, payload=payload,
                actor_id="admin", reason="test", action=action,
                request_id=f"req-{collection}",
            )
        _, _, outsider_header = _request(
            f"{base_url}/auth/login",
            body={
                "username": "outsider", "password": "safe-password",
                "session_mode": "cookie",
            },
        )
        outsider_cookie = outsider_header.split(";", 1)[0] if outsider_header else ""
        outsider_status, outsider_listing, _ = _request(
            f"{base_url}/api/v1/reports", cookie=outsider_cookie,
        )
        assert outsider_status == 200
        assert outsider_listing["reports"] == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
