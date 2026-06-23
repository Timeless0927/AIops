"""ISSUE-B: 真根因回填端点(评测集标签 C)。

POST /api/case-profile 回填确认根因 → GET /api/case-profile 读回 → 校验必填字段缺失被拒。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from asyncio import run as asyncio_run
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from toolsets import incident_store
from apps.aiops_k8s_gateway import main as gateway_main


def _write_identity_config(path: Path) -> None:
    path.write_text(
        f"""
identity:
  store_path: "{path.with_name("identity.db")}"
  ldap:
    enabled: false
  users:
    - username: alice
      password: alice-pass
      display_name: Alice
      roles: [user]
      scope:
        namespaces: ["prod", "default"]
""",
        encoding="utf-8",
    )


def _request_json(
    url: str,
    *,
    body: dict | None = None,
    token: str | None = None,
    method: str = "POST",
) -> tuple[int, dict]:
    data = json.dumps(body or {}).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


@pytest.fixture
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config_path = tmp_path / "identity.yaml"
    _write_identity_config(config_path)
    monkeypatch.setenv("AIOPS_IDENTITY_CONFIG", str(config_path))
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path / "data"))
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    old_store = incident_store._STORE
    old_audit_db = gateway_main.audit_log._DB
    incident_store._STORE = incident_store.IncidentStore(data_dir / "incidents.db")
    gateway_main.audit_log._DB = gateway_main.audit_log.AuditLogDB(data_dir / "audit_log.db")
    gateway_main._SESSIONS.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        gateway_main._SESSIONS.clear()
        incident_store._STORE.close()
        incident_store._STORE = old_store
        gateway_main.audit_log._DB.close()
        gateway_main.audit_log._DB = old_audit_db


def _login(gateway_url: str) -> str:
    status, payload = _request_json(
        f"{gateway_url}/auth/login",
        body={"username": "alice", "password": "alice-pass"},
    )
    assert status == 200
    return str(payload["token"])


def _backfill_payload(incident_id: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "incident_id": incident_id,
        "final_root_cause": "DB 连接池耗尽,新连接全超时",
        "root_cause_category": "connection_pool_exhaustion",
        "key_evidence_refs": ["ev-prom-1", "ev-log-3"],
        "effective_actions": ["调大连接池上限并重启 worker"],
    }
    payload.update(overrides)
    return payload


def _create_incident() -> str:
    return asyncio_run(
        incident_store.create_incident(
            "DBConnectionTimeout",
            "prod",
            "cluster-a",
            "数据库连接超时",
        )
    )


def test_backfill_and_read_back_round_trip(gateway: str) -> None:
    incident_id = _create_incident()
    token = _login(gateway)

    post_status, posted = _request_json(
        f"{gateway}/api/case-profile",
        body=_backfill_payload(incident_id),
        token=token,
    )
    assert post_status == 200
    assert posted["ok"] is True
    assert posted["incident_id"] == incident_id
    assert posted["incident_signature"]

    read_status, read_payload = _request_json(
        f"{gateway}/api/case-profile?incident_id={urllib.parse.quote(incident_id)}",
        token=token,
        method="GET",
    )
    assert read_status == 200
    profile = read_payload["case_profile"]
    assert profile["incident_id"] == incident_id
    assert profile["final_root_cause"] == "DB 连接池耗尽,新连接全超时"
    # root_cause_category 是评测集带容差打分的锚点,必须原样读回
    assert profile["root_cause_category"] == "connection_pool_exhaustion"
    assert profile["key_evidence_refs"] == ["ev-prom-1", "ev-log-3"]
    assert profile["effective_actions"] == ["调大连接池上限并重启 worker"]


@pytest.mark.parametrize(
    "missing_field",
    ["final_root_cause", "root_cause_category", "incident_id"],
)
def test_backfill_rejects_missing_required_fields(gateway: str, missing_field: str) -> None:
    incident_id = _create_incident()
    token = _login(gateway)

    payload = _backfill_payload(incident_id)
    if missing_field == "incident_id":
        payload[missing_field] = ""
    else:
        del payload[missing_field]

    status, response = _request_json(
        f"{gateway}/api/case-profile",
        body=payload,
        token=token,
    )
    assert status == 400
    assert response["ok"] is False
    assert response["status"] == "invalid"
    assert response["error"]


def test_backfill_rejects_nonexistent_incident(gateway: str) -> None:
    token = _login(gateway)
    status, response = _request_json(
        f"{gateway}/api/case-profile",
        body=_backfill_payload("inc-does-not-exist"),
        token=token,
    )
    assert status == 404
    assert response["status"] == "not_found"


def test_read_back_unknown_incident_is_not_found(gateway: str) -> None:
    token = _login(gateway)
    status, response = _request_json(
        f"{gateway}/api/case-profile?incident_id=inc-missing",
        token=token,
        method="GET",
    )
    assert status == 404