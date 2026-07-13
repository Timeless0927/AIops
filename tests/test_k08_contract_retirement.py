"""K08 replacement contract and legacy mutation absence."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from apps.aiops_k8s_gateway import main as gateway_main
from apps.aiops_k8s_gateway.evidence_decision_schema import MIGRATIONS
from apps.aiops_k8s_gateway.gateway_db import GatewayDatabase


def test_legacy_approval_routes_and_schemas_are_absent_from_openapi() -> None:
    spec = json.loads(Path("api/openapi/gateway-v1.json").read_text())
    paths = spec["paths"]
    schemas = spec["components"]["schemas"]

    assert "/api/v1/incidents/{id}/change-requests" in paths
    assert "/api/v1/admin/kubernetes-change-authorities" in paths
    assert not any("approve-and-execute" in path for path in paths)
    assert not any("approval-authorities" in path for path in paths)
    assert {
        "ApprovalAuthority",
        "ApprovalAuthorityCreateRequest",
        "ApprovalAuthorityUpdateRequest",
        "ApprovalAuthorityListResponse",
        "ApprovalAuthorityResponse",
        "ApproveAndExecuteRequest",
        "GovernedExecution",
        "ApproveAndExecuteResponse",
    }.isdisjoint(schemas)
    recommendation = schemas["RecommendedAction"]
    assert set(recommendation["required"]) == {
        "id", "version", "summary", "change_intent", "target", "evidence_step_ids",
        "safeguards", "gate", "hash", "stale", "expires_at",
    }
    assert {"action_type", "parameters", "rollback_plan", "can_approve", "approval_id", "execution"}.isdisjoint(
        recommendation["properties"],
    )
    command = schemas["ConnectorCommand"]
    assert command["properties"]["action"]["enum"] == [
        "get_resource", "validate_kubernetes_change", "execute_kubernetes_change",
        "reconcile_kubernetes_change",
    ]
    assert {"frozen_action", "scale_replica_bounds", "rollback_plan"}.isdisjoint(command["properties"])


def test_legacy_gateway_approval_owner_and_adapter_are_deleted() -> None:
    gateway = Path("apps/aiops_k8s_gateway")
    assert not (gateway / "approval.py").exists()
    assert not (gateway / "approval_http.py").exists()
    assert not Path("runtime/approval_execution_worker.py").exists()
    assert not Path("toolsets/feishu_native_approval.py").exists()
    assert not Path("toolsets/remediation_plan.py").exists()

    main = (gateway / "main.py").read_text()
    incident_http = (gateway / "incident_http.py").read_text()
    assert "    approval_http," not in main
    assert "or approval_http.dispatch" not in main
    assert "def _approvals(" not in main
    assert "project_workbench" not in incident_http


def test_typed_deployment_mutation_branch_is_deleted() -> None:
    gateway_commands = Path("apps/aiops_k8s_gateway/connector_commands.py").read_text()
    worker = Path("apps/cluster_connector/command_worker.py").read_text()
    kubectl = Path("apps/cluster_connector/kubectl_executor.py").read_text()
    dockerfile = Path("Dockerfile.aiops").read_text()
    manifests = "\n".join(
        path.read_text() for path in Path("deploy/k8s").rglob("*.yaml")
    )

    assert not Path("apps/cluster_connector/deployment_mutations.py").exists()
    assert "queue_mutation" not in gateway_commands
    assert "execute_mutation_command" not in worker
    assert "_validate_mutation_allowlist" not in kubectl
    assert "approval_execution_worker" not in dockerfile
    assert "AIOPS_CONNECTOR_ENABLE_MUTATION_EXECUTION" not in manifests
    for action in ("restart_deployment", "scale_deployment", "rollback_deployment"):
        assert action not in gateway_commands
        assert action not in worker


def test_latest_gateway_schema_contains_only_generic_change_contract(tmp_path: Path) -> None:
    assert gateway_main.GatewayHandler.service_name
    database = GatewayDatabase(tmp_path / "gateway.db")
    with database.connect() as conn:
        command_columns = {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(connector_commands)")
        }
        command_sql = str(conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'connector_commands'"
        ).fetchone()[0])
        recommendation_columns = {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(recommended_actions)")
        }
        tables = {
            str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }

    assert {"rollback_plan_json", "execution_grant_id"}.isdisjoint(command_columns)
    assert all(action not in command_sql for action in (
        "restart_deployment", "scale_deployment", "rollback_deployment",
    ))
    assert {"approval_authorities", "approvals", "execution_grants"}.isdisjoint(tables)
    assert recommendation_columns == {
        "id", "investigation_id", "version", "summary", "change_intent", "target_json",
        "evidence_step_ids_json", "safeguards_json", "gate_status",
        "gate_reasons_json", "action_hash", "stale", "created_at",
    }


def test_recommendation_migration_preserves_only_guidance_fields() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE investigations (id TEXT PRIMARY KEY);
        INSERT INTO investigations VALUES ('investigation-1');
        CREATE TABLE recommended_actions (
            id TEXT NOT NULL, investigation_id TEXT NOT NULL, version INTEGER NOT NULL,
            action_type TEXT NOT NULL, summary TEXT NOT NULL, target_json TEXT NOT NULL,
            parameters_json TEXT NOT NULL, evidence_step_ids_json TEXT NOT NULL,
            safeguards_json TEXT NOT NULL, rollback_plan_json TEXT NOT NULL,
            gate_status TEXT NOT NULL, gate_reasons_json TEXT NOT NULL,
            action_hash TEXT NOT NULL, stale INTEGER NOT NULL, created_at REAL NOT NULL,
            PRIMARY KEY (investigation_id, id, version)
        );
        CREATE INDEX recommended_actions_by_investigation
            ON recommended_actions(investigation_id, version, id);
        INSERT INTO recommended_actions VALUES (
            'recommendation-1', 'investigation-1', 1, 'restart_deployment',
            'Restore checkout availability', '{}', '{}', '["evidence-1"]',
            '["verify readiness"]', '{"type":"none"}', 'complete', '[]',
            'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 0, 1000
        );
    """)

    conn.executescript(MIGRATIONS[1][1])

    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(recommended_actions)")}
    row = conn.execute(
        "SELECT summary, change_intent, evidence_step_ids_json, safeguards_json "
        "FROM recommended_actions",
    ).fetchone()
    assert columns == {
        "id", "investigation_id", "version", "summary", "change_intent", "target_json",
        "evidence_step_ids_json", "safeguards_json", "gate_status",
        "gate_reasons_json", "action_hash", "stale", "created_at",
    }
    assert row == (
        "Restore checkout availability", "controlled_restart", '["evidence-1"]',
        '["verify readiness"]',
    )
