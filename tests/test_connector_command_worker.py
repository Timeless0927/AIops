from pathlib import Path
import hashlib
import json
import sqlite3
import time

import pytest

from apps.cluster_connector import command_worker
from apps.cluster_connector.deployment_mutations import build_mutation_envelopes
from apps.cluster_connector.command_worker import ConnectorCommandJournal, build_read_envelope


def _governed(command: dict[str, object]) -> dict[str, object]:
    parameters = command["parameters"]
    assert isinstance(parameters, dict)
    frozen = {
        "action_type": command["action"],
        "target": {
            "cluster_id": command["cluster_id"], "namespace": command["namespace"],
            "workload_kind": "Deployment", "workload_name": parameters["deployment_name"],
            "deployment_target_id": "target-1", "resource_binding_id": "binding-1", "binding_revision": 1,
        },
        "typed_parameters": {
            key: value for key, value in parameters.items() if key not in {"resource_kind", "deployment_name"}
        },
        "evidence_step_ids": ["step-k8s"], "safeguards": ["one Deployment"],
        "rollback_plan": command.get("rollback_plan"),
    }
    return {
        **command, "frozen_action": frozen,
        "scale_replica_bounds": [0, 20] if command["action"] == "scale_deployment" else None,
        "action_hash": hashlib.sha256(json.dumps(frozen, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
    }


def test_connector_journal_recovers_unreported_terminal_result(tmp_path: Path) -> None:
    path = tmp_path / "connector.db"
    command = {
        "id": "command-1",
        "cluster_id": "cluster-prod",
        "namespace": "payments",
        "action": "get_resource",
        "parameters": {"resource_kind": "pods", "output": "json"},
        "lease_id": "lease-1",
    }
    journal = ConnectorCommandJournal(path)
    journal.accept(command)
    journal.started("command-1")
    journal.terminal("command-1", {"status": "succeeded", "stdout": '{"items":[]}'})

    recovered = ConnectorCommandJournal(path).unreported_results()

    assert recovered == [(command, {"status": "succeeded", "stdout": '{"items":[]}'})]


def test_connector_cleanup_expires_only_acknowledged_journal_and_stale_locks(tmp_path: Path) -> None:
    now = [1_800_000_000.0]
    journal = ConnectorCommandJournal(tmp_path / "connector.db", clock=lambda: now[0])
    command = {
        "id": "command-old",
        "cluster_id": "cluster-prod",
        "namespace": "payments",
        "action": "get_resource",
        "parameters": {"resource_kind": "pods", "output": "json"},
        "lease_id": "lease-old",
    }
    journal.accept(command)
    journal.started("command-old")
    journal.terminal("command-old", {"status": "succeeded"})
    journal.acknowledged("command-old")
    assert journal.acquire_execution_lock("cluster-prod/payments/Deployment/api", "command-old")

    now[0] += 30 * 24 * 60 * 60 + 1
    journal.accept({**command, "id": "command-terminal", "lease_id": "lease-terminal"})
    journal.started("command-terminal")
    journal.terminal("command-terminal", {"status": "succeeded"})
    journal.accept({**command, "id": "command-recent", "lease_id": "lease-recent"})
    journal.started("command-recent")
    journal.terminal("command-recent", {"status": "succeeded"})
    journal.acknowledged("command-recent")

    assert journal.cleanup_expired() == {"journal": 1, "locks": 1}
    assert 'state="acknowledged"} 1' in journal.metrics(now=now[0])
    assert journal.unreported_result("command-terminal") == {"status": "succeeded"}
    journal.accept({**command, "id": "command-new", "lease_id": "lease-new"})
    assert journal.acquire_execution_lock("cluster-prod/payments/Deployment/api", "command-new")


def test_connector_database_forward_migrates_existing_journal(tmp_path: Path) -> None:
    path = tmp_path / "connector.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE command_journal (
                command_id TEXT PRIMARY KEY,
                command_json TEXT NOT NULL CHECK (json_valid(command_json)),
                state TEXT NOT NULL CHECK (state IN ('accepted', 'started', 'terminal', 'acknowledged')),
                result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
                updated_at REAL NOT NULL
            );
            CREATE TABLE execution_locks (
                scope TEXT PRIMARY KEY,
                command_id TEXT NOT NULL UNIQUE,
                acquired_at REAL NOT NULL
            );
            INSERT INTO command_journal VALUES (
                'command-old', '{"id":"command-old"}', 'terminal', '{"status":"succeeded"}', 1
            );
            INSERT INTO execution_locks VALUES ('cluster/ns/Deployment/api', 'command-old', 1);
            """
        )

    journal = ConnectorCommandJournal(path, clock=lambda: 10_000.0)

    assert journal.unreported_result("command-old") == {"status": "succeeded"}
    assert journal.cleanup_expired() == {"journal": 0, "locks": 1}
    with pytest.raises(sqlite3.IntegrityError):
        journal.acquire_execution_lock("cluster/ns/Deployment/missing", "command-missing")


def test_connector_builds_only_typed_read_envelopes() -> None:
    command = {
        "id": "command-1",
        "cluster_id": "cluster-prod",
        "namespace": "payments",
        "action": "get_resource",
        "parameters": {"resource_kind": "pods", "selector": "app=api", "output": "json"},
        "lease_id": "lease-1",
    }
    envelope = build_read_envelope(command)
    assert envelope.argv == (
        "kubectl", "get", "pods", "--selector", "app=api", "--namespace", "payments", "--output", "json"
    )

    with pytest.raises(ValueError, match="unsupported read action"):
        build_read_envelope({**command, "action": "shell", "parameters": {"argv": ["kubectl", "delete", "pods"]}})
    with pytest.raises(ValueError, match="events do not accept"):
        build_read_envelope({**command, "parameters": {"resource_kind": "events", "name": "event-1"}})
    with pytest.raises(ValueError, match="invalid selector"):
        build_read_envelope({**command, "parameters": {"resource_kind": "pods", "selector": ""}})
    with pytest.raises(ValueError, match="unsupported read parameters"):
        build_read_envelope({**command, "parameters": {"resource_kind": ["pods"]}})


def test_worker_rejects_plaintext_non_loopback_gateway(tmp_path: Path) -> None:
    assert not command_worker.run_command_cycle(
        "http://gateway.internal",
        connector_id="connector-prod",
        cluster_id="cluster-prod",
        credential="credential",
        allowed_namespaces={"payments"},
        journal=ConnectorCommandJournal(tmp_path / "connector.db"),
        wait_seconds=0,
    )


def test_worker_executes_only_after_gateway_acknowledges_start(tmp_path: Path, monkeypatch) -> None:
    command = {
        "id": "command-1",
        "cluster_id": "cluster-prod",
        "namespace": "payments",
        "action": "get_resource",
        "parameters": {"resource_kind": "pods", "output": "json"},
        "status": "leased",
        "attempt_count": 1,
        "lease_id": "lease-1",
        "lease_expires_at": 200,
        "created_at": 100,
        "result": None,
    }
    calls: list[str] = []

    def post(_url, path, _payload, _credential, **_kwargs):
        calls.append(path)
        if path.endswith("/poll"):
            return 200, {"command": command}
        return 200, {}

    def execute(*_args, **_kwargs):
        calls.append("execute")
        return {
            "status": "succeeded", "stdout": "{}", "stderr": "", "exit_code": 0,
            "truncated": False, "error_code": None, "error_message": None,
        }

    monkeypatch.setattr(command_worker, "_post_json", post)
    monkeypatch.setattr(command_worker, "execute_read_command", execute)
    journal = ConnectorCommandJournal(tmp_path / "connector.db")

    assert command_worker.run_command_cycle(
        "https://gateway.example",
        connector_id="connector-prod",
        cluster_id="cluster-prod",
        credential="credential",
        allowed_namespaces={"payments"},
        journal=journal,
        wait_seconds=0,
    )
    assert calls == [
        "/api/v1/connectors/commands/poll",
        "/api/v1/connectors/commands/command-1/start",
        "execute",
        "/api/v1/connectors/commands/command-1/result",
    ]
    assert journal.unreported_results() == []


def test_restart_uses_exact_preflight_execution_and_post_check(tmp_path: Path, monkeypatch) -> None:
    command = _governed({
        "id": "command-restart", "cluster_id": "cluster-prod", "namespace": "payments",
        "action": "restart_deployment", "parameters": {"resource_kind": "Deployment", "deployment_name": "checkout-api"},
        "execution_grant_id": "grant-1", "execution_grant_expires_at": time.time() + 60, "action_hash": "a" * 64, "status": "leased", "attempt_count": 0,
        "lease_id": "lease-1", "lease_expires_at": 200, "created_at": 100, "result": None,
    })
    envelopes = build_mutation_envelopes(command, now=time.time())
    assert [envelope.action_type for envelope in envelopes] == ["read", "mutation", "read"]
    assert envelopes[1].argv == (
        "kubectl", "rollout", "restart", "deployment/checkout-api", "--namespace", "payments"
    )
    calls: list[str] = []

    class Result:
        def to_dict(self):
            return {"status": "succeeded", "stdout": "", "stderr": "", "exit_code": 0, "truncated": False, "error_code": None, "error_message": None}

    monkeypatch.setattr(command_worker, "execute_command_envelope", lambda envelope, **_kwargs: calls.append(envelope.command_id) or Result())
    result = command_worker.execute_mutation_command(
        command, connector_id="connector-prod", cluster_id="cluster-prod", allowed_namespaces={"payments"},
        clock=time.time, executor=command_worker.execute_command_envelope,
    )
    journal = ConnectorCommandJournal(tmp_path / "connector.db")
    journal.accept(command)
    assert journal.acquire_execution_lock("cluster-prod/payments/Deployment/checkout-api", "command-restart")
    assert not journal.acquire_execution_lock("cluster-prod/payments/Deployment/checkout-api", "other-command")
    assert result["status"] == "succeeded"
    assert calls == ["command-restart:preflight", "command-restart:execute", "command-restart:post-check"]


@pytest.mark.parametrize(
    ("action", "parameters", "expected_argv"),
    [
        (
            "scale_deployment",
            {"current_replicas": 3, "target_replicas": 5},
            ("kubectl", "scale", "deployment/checkout-api", "--replicas", "5", "--namespace", "payments"),
        ),
        (
            "rollback_deployment",
            {"target_revision": 41},
            ("kubectl", "rollout", "undo", "deployment/checkout-api", "--to-revision", "41", "--namespace", "payments"),
        ),
    ],
)
def test_connector_builds_only_bounded_typed_deployment_mutations(
    action: str, parameters: dict[str, int], expected_argv: tuple[str, ...]
) -> None:
    command = _governed({
        "id": "command-mutation", "cluster_id": "cluster-prod", "namespace": "payments",
        "action": action,
        "parameters": {"resource_kind": "Deployment", "deployment_name": "checkout-api", **parameters},
        "rollback_plan": None,
        "execution_grant_id": "grant-1", "execution_grant_expires_at": time.time() + 60,
        "action_hash": "a" * 64, "status": "leased", "attempt_count": 0,
        "lease_id": "lease-1", "lease_expires_at": 200, "created_at": 100, "result": None,
    })

    envelopes = build_mutation_envelopes(command, now=time.time())

    assert envelopes[1].argv == expected_argv
    with pytest.raises(ValueError, match="unsupported mutation action"):
        build_mutation_envelopes({**command, "action": "shell"}, now=time.time())
    with pytest.raises(ValueError, match="action hash"):
        build_mutation_envelopes({**command, "action_hash": "0" * 64}, now=time.time())
    if action == "scale_deployment":
        with pytest.raises(ValueError, match="replica bounds"):
            build_mutation_envelopes(_governed({
                **command,
                "parameters": {**command["parameters"], "target_replicas": 21},
            }), now=time.time())
        with pytest.raises(ValueError, match="replica bounds"):
            build_mutation_envelopes({**command, "scale_replica_bounds": [1, 4]}, now=time.time())


def test_post_check_runs_only_frozen_conditional_rollback_when_assumptions_match(monkeypatch) -> None:
    command = _governed({
        "id": "command-scale", "cluster_id": "cluster-prod", "namespace": "payments",
        "action": "scale_deployment",
        "parameters": {
            "resource_kind": "Deployment", "deployment_name": "checkout-api",
            "current_replicas": 3, "target_replicas": 5,
        },
        "rollback_plan": {
            "condition": "post_check_failed", "action_type": "scale_deployment",
            "parameters": {"current_replicas": 5, "target_replicas": 3},
            "target_assumptions": {"replicas": 5},
        },
        "execution_grant_id": "grant-1", "execution_grant_expires_at": 101.0,
        "action_hash": "a" * 64, "status": "leased", "attempt_count": 0,
        "lease_id": "lease-1", "lease_expires_at": 200, "created_at": 100, "result": None,
    })
    outputs = iter([
        ("succeeded", '{"spec":{"replicas":3}}'),
        ("succeeded", ""),
        ("failed", ""),
        ("succeeded", '{"spec":{"replicas":5}}'),
        ("succeeded", ""),
        ("succeeded", '{"spec":{"replicas":3}}'),
    ])
    calls: list[tuple[str, ...]] = []

    class Result:
        def __init__(self, status: str, stdout: str) -> None:
            self.status = status
            self.stdout = stdout

        def to_dict(self):
            return {
                "status": self.status, "stdout": self.stdout, "stderr": "", "exit_code": 0,
                "truncated": False, "error_code": None, "error_message": None,
            }

    def execute(envelope, **_kwargs):
        calls.append(envelope.argv)
        return Result(*next(outputs))

    monkeypatch.setattr(command_worker, "execute_command_envelope", execute)

    clock_values = iter([100.0, 100.0, 102.0])
    result = command_worker.execute_mutation_command(
        command, connector_id="connector-prod", cluster_id="cluster-prod", allowed_namespaces={"payments"},
        clock=lambda: next(clock_values), executor=command_worker.execute_command_envelope,
    )

    assert result["status"] == "failed"
    assert result["error_code"] == "rolled_back"
    assert calls[4] == (
        "kubectl", "scale", "deployment/checkout-api", "--replicas", "3", "--namespace", "payments"
    )

    changed_outputs = iter([
        ("succeeded", '{"spec":{"replicas":3}}'), ("succeeded", ""), ("failed", ""),
        ("succeeded", '{"spec":{"replicas":4}}'),
    ])
    monkeypatch.setattr(
        command_worker, "execute_command_envelope",
        lambda _envelope, **_kwargs: Result(*next(changed_outputs)),
    )
    changed = command_worker.execute_mutation_command(
        command, connector_id="connector-prod", cluster_id="cluster-prod", allowed_namespaces={"payments"},
        clock=lambda: 100.0, executor=command_worker.execute_command_envelope,
    )
    assert changed["error_code"] == "rollback_required"


def test_revision_rollback_checks_explicit_history_then_rollout_status(monkeypatch) -> None:
    command = _governed({
        "id": "command-rollback", "cluster_id": "cluster-prod", "namespace": "payments",
        "action": "rollback_deployment",
        "parameters": {"resource_kind": "Deployment", "deployment_name": "checkout-api", "target_revision": 41},
        "rollback_plan": None,
        "execution_grant_id": "grant-1", "execution_grant_expires_at": time.time() + 60,
        "action_hash": "a" * 64, "status": "leased", "attempt_count": 0,
        "lease_id": "lease-1", "lease_expires_at": 200, "created_at": 100, "result": None,
    })
    outputs = iter(["revision 41 exists", "", "deployment successfully rolled out"])

    class Result:
        def to_dict(self):
            return {
                "status": "succeeded", "stdout": next(outputs), "stderr": "", "exit_code": 0,
                "truncated": False, "error_code": None, "error_message": None,
            }

    monkeypatch.setattr(command_worker, "execute_command_envelope", lambda _envelope, **_kwargs: Result())

    result = command_worker.execute_mutation_command(
        command, connector_id="connector-prod", cluster_id="cluster-prod", allowed_namespaces={"payments"},
        clock=time.time, executor=command_worker.execute_command_envelope,
    )

    assert result["status"] == "succeeded"


def test_execution_grant_is_rechecked_after_preflight(monkeypatch) -> None:
    now = [100.0]
    command = _governed({
        "id": "command-scale", "cluster_id": "cluster-prod", "namespace": "payments",
        "action": "scale_deployment",
        "parameters": {
            "resource_kind": "Deployment", "deployment_name": "checkout-api",
            "current_replicas": 3, "target_replicas": 5,
        },
        "rollback_plan": None,
        "execution_grant_id": "grant-1", "execution_grant_expires_at": 101.0,
        "action_hash": "a" * 64, "status": "leased", "attempt_count": 0,
        "lease_id": "lease-1", "lease_expires_at": 200, "created_at": 100, "result": None,
    })
    calls: list[tuple[str, ...]] = []

    class Result:
        def to_dict(self):
            now[0] = 102.0
            return {
                "status": "succeeded", "stdout": '{"spec":{"replicas":3}}', "stderr": "", "exit_code": 0,
                "truncated": False, "error_code": None, "error_message": None,
            }

    monkeypatch.setattr(
        command_worker, "execute_command_envelope",
        lambda envelope, **_kwargs: calls.append(envelope.argv) or Result(),
    )

    result = command_worker.execute_mutation_command(
        command, connector_id="connector-prod", cluster_id="cluster-prod",
        allowed_namespaces={"payments"}, clock=lambda: now[0],
        executor=command_worker.execute_command_envelope,
    )

    assert result["status"] == "rejected"
    assert result["error_code"] == "execution_grant_expired"
    assert len(calls) == 1
