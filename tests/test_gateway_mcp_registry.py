from __future__ import annotations

import json
from pathlib import Path

from apps.aiops_k8s_gateway.mcp_registry import MCPRegistry
from apps.aiops_k8s_gateway import connector_enrollments as _connector_enrollments  # noqa: F401


POLICY = [{"name": "query_metrics", "version": "prometheus-query-v1", "read_only": True}]
ALLOWED_SCOPE = [{"cluster_id": "cluster-a", "namespace": "payments"}]
HEALTH = {
    "status": "ok",
    "capabilities": [{
        "name": "query_metrics",
        "version": "prometheus-query-v1",
        "read_only": True,
        "mutation": False,
        "path": "/query_metrics",
    }],
}
FROZEN_SCOPE = {
    "resources": [{"cluster_id": "cluster-a", "namespace": "payments"}],
}


def _registry(tmp_path: Path) -> MCPRegistry:
    key = tmp_path / "mcp-key"
    key.write_bytes(b"k" * 32)
    return MCPRegistry(
        tmp_path / "gateway.db",
        key_path=key,
        clock=lambda: 1_700_000_000.0,
        id_factory=lambda: "mcp-1",
        revision_id=lambda: "mcp-revision-1",
        nonce_source=lambda size: b"n" * size,
    )


def test_registration_masks_and_encrypts_credential_and_audits_actor_request(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    created = registry.create(
        name="Prometheus production",
        endpoint="https://mcp.example.test",
        credential="mcp-plaintext-secret",
        capabilities=POLICY,
        allowed_scope=ALLOWED_SCOPE,
        enabled=True,
        actor_id="user:admin",
        reason="register approved metrics source",
        request_id="mcp-create-1",
    )

    assert created["credential_configured"] is True
    assert created["verification"] == {
        "state": "unverified", "reason_code": "verification_required", "verified_revision": None,
    }
    assert registry.list() == [created]
    serialized = json.dumps(created, ensure_ascii=False)
    assert "mcp-plaintext-secret" not in serialized

    with registry.database.connect() as conn:
        row = conn.execute(
            "SELECT credential_nonce, credential_ciphertext, credential_sha256 FROM mcp_integrations"
        ).fetchone()
        audit = conn.execute(
            "SELECT actor_id, action, reason, request_id, before_json, after_json FROM admin_audit "
            "WHERE target_type = 'mcp_integration'"
        ).fetchone()
    assert bytes(row["credential_ciphertext"]) != b"mcp-plaintext-secret"
    assert "mcp-plaintext-secret" not in json.dumps(dict(row), default=str)
    assert dict(audit) == {
        "actor_id": "user:admin",
        "action": "mcp_integration_create",
        "reason": "register approved metrics source",
        "request_id": "mcp-create-1",
        "before_json": None,
        "after_json": json.dumps(created, ensure_ascii=False, sort_keys=True),
    }


def test_verified_exact_read_only_capability_is_authorized_only_in_allowed_scope(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.create(
        name="Prometheus production", endpoint="https://mcp.example.test", credential=None,
        capabilities=POLICY, allowed_scope=ALLOWED_SCOPE, enabled=True,
        actor_id="user:admin", reason="register", request_id="mcp-create-1",
    )

    verified = registry.verify(
        "mcp-1", actor_id="user:admin", reason="verify exact capability",
        request_id="mcp-verify-1", probe=lambda *_args: HEALTH,
    )
    snapshot = registry.authorized_snapshot(
        FROZEN_SCOPE, actor_id="user:sre", request_id="chat-1",
    )

    assert verified["verification"] == {
        "state": "verified", "reason_code": None, "verified_revision": "mcp-revision-1",
    }
    assert verified["health"] == {"status": "ok", "checked_at": 1_700_000_000.0, "error": None}
    assert snapshot == {
        "query_metrics": {
            "name": "query_metrics",
            "version": "prometheus-query-v1",
            "enabled": True,
            "read_only": True,
            "mutation": False,
            "integration_id": "mcp-1",
            "integration_revision": "mcp-revision-1",
        }
    }
    assert registry.authorized_snapshot(
        {"resources": [{"cluster_id": "cluster-a", "namespace": "orders"}]},
        actor_id="user:sre", request_id="chat-2",
    ) == {}
    with registry.database.connect() as conn:
        actions = [
            (row["action"], row["result"], row["request_id"])
            for row in conn.execute(
                "SELECT action, result, request_id FROM admin_audit "
                "WHERE target_type = 'mcp_integration' ORDER BY id"
            )
        ]
    assert ("mcp_integration_use", "success", "chat-1") in actions
    assert ("mcp_integration_use_denied", "rejected", "chat-2") in actions


def test_changed_or_mutation_like_snapshot_fails_closed_and_preserves_comparison(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.create(
        name="Prometheus production", endpoint="https://mcp.example.test", credential=None,
        capabilities=POLICY, allowed_scope=ALLOWED_SCOPE, enabled=True,
        actor_id="user:admin", reason="register", request_id="mcp-create-1",
    )
    registry.verify(
        "mcp-1", actor_id="user:admin", reason="initial verification",
        request_id="mcp-verify-1", probe=lambda *_args: HEALTH,
    )
    changed_health = {
        **HEALTH,
        "capabilities": [{**HEALTH["capabilities"][0], "version": "prometheus-query-v2", "mutation": True}],
    }

    changed = registry.verify(
        "mcp-1", actor_id="user:admin", reason="detect drift",
        request_id="mcp-verify-2", probe=lambda *_args: changed_health,
    )

    assert changed["verification"]["state"] == "failed"
    assert changed["verification"]["reason_code"] == "capability_snapshot_changed"
    assert changed["capability_changed"] is True
    assert changed["verified_capability_snapshot"] == HEALTH["capabilities"]
    assert changed["capability_snapshot"] == changed_health["capabilities"]
    assert registry.authorized_snapshot(
        FROZEN_SCOPE, actor_id="user:sre", request_id="chat-after-drift",
    ) == {}


def test_update_disables_and_invalidates_revision_bound_verification(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.create(
        name="Prometheus production", endpoint="https://mcp.example.test", credential=None,
        capabilities=POLICY, allowed_scope=ALLOWED_SCOPE, enabled=True,
        actor_id="user:admin", reason="register", request_id="mcp-create-1",
    )
    registry.verify(
        "mcp-1", actor_id="user:admin", reason="verify",
        request_id="mcp-verify-1", probe=lambda *_args: HEALTH,
    )

    updated = registry.update(
        "mcp-1", name="Prometheus production", endpoint="https://mcp.example.test",
        credential=None, capabilities=POLICY, allowed_scope=ALLOWED_SCOPE, enabled=False,
        expected_revision="mcp-revision-1", actor_id="user:admin", reason="disable unsafe source",
        request_id="mcp-disable-1",
    )

    assert updated["enabled"] is False
    assert updated["verification"] == {
        "state": "unverified", "reason_code": "verification_required", "verified_revision": None,
    }
    assert registry.authorized_snapshot(
        FROZEN_SCOPE, actor_id="user:sre", request_id="investigation-1",
    ) == {}


def test_invoke_is_bound_to_verified_revision_scope_and_credential(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.create(
        name="Prometheus production", endpoint="https://mcp.example.test",
        credential="mcp-runtime-secret", capabilities=POLICY, allowed_scope=ALLOWED_SCOPE,
        enabled=True, actor_id="user:admin", reason="register", request_id="mcp-create-1",
    )
    registry.verify(
        "mcp-1", actor_id="user:admin", reason="verify", request_id="mcp-verify-1",
        probe=lambda *_args: HEALTH,
    )
    sent: list[tuple[object, ...]] = []

    result = registry.invoke(
        "query_metrics",
        {"cluster_id": "cluster-a", "namespace": "payments", "query": "up"},
        integration_id="mcp-1", integration_revision="mcp-revision-1",
        actor_id="system:serviceaccount:aiops:aiops-diagnosis", request_id="tool-call-1",
        send=lambda *args: sent.append(args) or {"status": "succeeded", "summary": "up=1"},
    )

    assert result == {"status": "succeeded", "summary": "up=1"}
    assert sent == [(
        "https://mcp.example.test", "/query_metrics", "mcp-runtime-secret",
        {"cluster_id": "cluster-a", "namespace": "payments", "query": "up"}, "tool-call-1",
    )]
    try:
        registry.invoke(
            "query_metrics",
            {"cluster_id": "cluster-a", "namespace": "orders", "query": "up"},
            integration_id="mcp-1", integration_revision="mcp-revision-1",
            actor_id="system:serviceaccount:aiops:aiops-diagnosis", request_id="tool-call-2",
            send=lambda *_args: {"status": "succeeded"},
        )
    except Exception as exc:
        assert getattr(exc, "code", None) == "capability_denied"
    else:
        raise AssertionError("out-of-scope MCP invocation was allowed")
    with registry.database.connect() as conn:
        audits = [dict(row) for row in conn.execute(
            "SELECT action, result, request_id FROM admin_audit "
            "WHERE action LIKE 'mcp_integration_invoke%' ORDER BY id"
        )]
    assert audits == [
        {"action": "mcp_integration_invoke", "result": "success", "request_id": "tool-call-1"},
        {"action": "mcp_integration_invoke_denied", "result": "rejected", "request_id": "tool-call-2"},
    ]
