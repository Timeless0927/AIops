from __future__ import annotations

from pathlib import Path

import pytest

from apps.aiops_k8s_gateway.mcp_registry import MCPRegistry
from apps.aiops_k8s_gateway.skill_registry import SkillRegistry, SkillRegistryError
from apps.aiops_k8s_gateway.v1_store import GatewayV1Store  # noqa: F401 - registers shared Gateway schema


SCOPE = [{"cluster_id": "cluster-a", "namespace": "payments"}]
REQUIRED_MCP = [{
    "integration_id": "mcp-1",
    "integration_revision": "mcp-revision-1",
    "name": "query_metrics",
    "version": "prometheus-query-v1",
}]


def _registry(tmp_path: Path) -> SkillRegistry:
    return SkillRegistry(
        tmp_path / "gateway.db",
        clock=lambda: 1_700_000_000.0,
        id_factory=lambda: "skill-1",
    )


def _verified_mcp(tmp_path: Path) -> MCPRegistry:
    key = tmp_path / "mcp-key"
    key.write_bytes(b"k" * 32)
    revisions = iter(("mcp-revision-1", "mcp-revision-2"))
    registry = MCPRegistry(
        tmp_path / "gateway.db",
        key_path=key,
        clock=lambda: 1_700_000_000.0,
        id_factory=lambda: "mcp-1",
        revision_id=lambda: next(revisions),
    )
    registry.create(
        name="Prometheus production",
        endpoint="https://mcp.example.test",
        credential=None,
        capabilities=[{"name": "query_metrics", "version": "prometheus-query-v1", "read_only": True}],
        allowed_scope=SCOPE,
        enabled=True,
        actor_id="user:admin",
        reason="register metrics",
        request_id="mcp-create-1",
    )
    registry.verify(
        "mcp-1",
        actor_id="user:admin",
        reason="verify metrics",
        request_id="mcp-verify-1",
        probe=lambda *_args: {
            "status": "ok",
            "capabilities": [{
                "name": "query_metrics",
                "version": "prometheus-query-v1",
                "read_only": True,
                "mutation": False,
                "path": "/query_metrics",
            }],
        },
    )
    return registry


def test_create_and_version_preserve_immutable_history(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    created = registry.create(
        name="Payments triage",
        instruction="Check service health before drawing a conclusion.",
        workflow=["Query metrics", "Compare logs", "Summarize cited facts"],
        applicable_scope=SCOPE,
        required_mcp=REQUIRED_MCP,
        actor_id="user:admin",
        reason="capture approved triage practice",
        request_id="skill-create-1",
    )
    versioned = registry.create_version(
        "skill-1",
        instruction="Check error rate before drawing a conclusion.",
        workflow=["Query metrics", "Compare logs", "Summarize cited facts"],
        applicable_scope=SCOPE,
        required_mcp=REQUIRED_MCP,
        actor_id="user:admin",
        reason="clarify the first check",
        request_id="skill-version-2",
    )

    assert created["enabled"] is False
    assert created["active_version"] is None
    assert [item["version"] for item in versioned["versions"]] == [1, 2]
    assert versioned["versions"][0]["instruction"] == "Check service health before drawing a conclusion."
    assert versioned["versions"][1]["instruction"] == "Check error rate before drawing a conclusion."
    assert registry.list() == [versioned]


def test_enable_validates_exact_dependencies_and_switches_versions(tmp_path: Path) -> None:
    mcp = _verified_mcp(tmp_path)
    registry = _registry(tmp_path)
    registry.create(
        name="Payments triage",
        instruction="Check service health.",
        workflow=[],
        applicable_scope=SCOPE,
        required_mcp=REQUIRED_MCP,
        actor_id="user:admin",
        reason="create practice",
        request_id="skill-create-1",
    )
    registry.create_version(
        "skill-1",
        instruction="Check error rate first.",
        workflow=[],
        applicable_scope=SCOPE,
        required_mcp=REQUIRED_MCP,
        actor_id="user:admin",
        reason="tighten practice",
        request_id="skill-version-2",
    )

    enabled_v1 = registry.set_enabled(
        "skill-1",
        version=1,
        expected_active_version=None,
        mcp_integrations=mcp.list(),
        actor_id="user:admin",
        reason="enable reviewed version",
        request_id="skill-enable-v1",
    )
    enabled_v2 = registry.set_enabled(
        "skill-1",
        version=2,
        expected_active_version=1,
        mcp_integrations=mcp.list(),
        actor_id="user:admin",
        reason="switch reviewed version",
        request_id="skill-enable-v2",
    )
    disabled = registry.set_enabled(
        "skill-1",
        version=None,
        expected_active_version=2,
        mcp_integrations=mcp.list(),
        actor_id="user:admin",
        reason="disable practice",
        request_id="skill-disable",
    )

    assert enabled_v1["active_version"] == 1
    assert enabled_v2["active_version"] == 2
    assert disabled["enabled"] is False
    assert [item["version"] for item in disabled["versions"]] == [1, 2]
    with registry.database.connect() as conn:
        actions = [
            (row["action"], row["request_id"])
            for row in conn.execute(
                "SELECT action, request_id FROM admin_audit WHERE target_type = 'skill' ORDER BY id"
            )
        ]
    assert actions[-3:] == [
        ("skill_enable", "skill-enable-v1"),
        ("skill_enable", "skill-enable-v2"),
        ("skill_disable", "skill-disable"),
    ]


def test_new_use_fails_closed_after_mcp_disable_without_rewriting_history(tmp_path: Path) -> None:
    mcp = _verified_mcp(tmp_path)
    registry = _registry(tmp_path)
    registry.create(
        name="Payments triage",
        instruction="Check the required observations.",
        workflow=["Query metrics"],
        applicable_scope=SCOPE,
        required_mcp=REQUIRED_MCP,
        actor_id="user:admin",
        reason="create practice",
        request_id="skill-create-1",
    )
    registry.set_enabled(
        "skill-1",
        version=1,
        expected_active_version=None,
        mcp_integrations=mcp.list(),
        actor_id="user:admin",
        reason="enable practice",
        request_id="skill-enable-1",
    )
    frozen_scope = {"resources": SCOPE}
    before = registry.authorized_bindings(
        frozen_scope,
        mcp.authorized_snapshot(frozen_scope, actor_id="user:sre", request_id="chat-1"),
        actor_id="user:sre",
        request_id="chat-1",
    )

    integration = mcp.list()[0]
    mcp.update(
        "mcp-1",
        name=integration["name"],
        endpoint=integration["endpoint"],
        credential=None,
        capabilities=integration["capabilities"],
        allowed_scope=integration["allowed_scope"],
        enabled=False,
        expected_revision=integration["revision"],
        actor_id="user:admin",
        reason="disable dependency",
        request_id="mcp-disable-1",
    )
    after = registry.authorized_bindings(
        frozen_scope,
        mcp.authorized_snapshot(frozen_scope, actor_id="user:sre", request_id="chat-2"),
        actor_id="user:sre",
        request_id="chat-2",
    )

    assert before == [{
        "id": "skill-1",
        "name": "Payments triage",
        "version": 1,
        "instruction": "Check the required observations.",
        "workflow": ["Query metrics"],
        "required_mcp": REQUIRED_MCP,
    }]
    assert after == []
    assert before[0]["version"] == registry.get("skill-1")["versions"][0]["version"]
    with registry.database.connect() as conn:
        audits = [dict(row) for row in conn.execute(
            "SELECT action, result, request_id FROM admin_audit "
            "WHERE target_type = 'skill' AND action LIKE 'skill_use%' ORDER BY id"
        )]
    assert audits == [
        {"action": "skill_use", "result": "success", "request_id": "chat-1"},
        {"action": "skill_use_denied", "result": "rejected", "request_id": "chat-2"},
    ]


def test_enable_dependency_failure_stays_disabled_and_is_audited(tmp_path: Path) -> None:
    mcp = _verified_mcp(tmp_path)
    registry = _registry(tmp_path)
    registry.create(
        name="Payments triage",
        instruction="Check metrics.",
        workflow=[],
        applicable_scope=SCOPE,
        required_mcp=[{**REQUIRED_MCP[0], "integration_revision": "stale-revision"}],
        actor_id="user:admin",
        reason="create practice",
        request_id="skill-create-1",
    )

    with pytest.raises(SkillRegistryError, match="unavailable or changed") as caught:
        registry.set_enabled(
            "skill-1",
            version=1,
            expected_active_version=None,
            mcp_integrations=mcp.list(),
            actor_id="user:admin",
            reason="attempt enable",
            request_id="skill-enable-denied",
        )

    assert caught.value.code == "skill_dependency_unavailable"
    assert registry.get("skill-1")["enabled"] is False
    with registry.database.connect() as conn:
        audit = dict(conn.execute(
            "SELECT actor_id, action, result, request_id FROM admin_audit "
            "WHERE action = 'skill_enable_denied'"
        ).fetchone())
    assert audit == {
        "actor_id": "user:admin",
        "action": "skill_enable_denied",
        "result": "rejected",
        "request_id": "skill-enable-denied",
    }
