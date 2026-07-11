from pathlib import Path
import time

import pytest

from apps.cluster_connector import command_worker
from apps.cluster_connector.command_worker import ConnectorCommandJournal, build_read_envelope


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
    command = {
        "id": "command-restart", "cluster_id": "cluster-prod", "namespace": "payments",
        "action": "restart_deployment", "parameters": {"resource_kind": "Deployment", "deployment_name": "checkout-api"},
        "execution_grant_id": "grant-1", "execution_grant_expires_at": time.time() + 60, "action_hash": "a" * 64, "status": "leased", "attempt_count": 0,
        "lease_id": "lease-1", "lease_expires_at": 200, "created_at": 100, "result": None,
    }
    envelopes = command_worker.build_restart_envelopes(command)
    assert [envelope.action_type for envelope in envelopes] == ["read", "mutation", "read"]
    assert envelopes[1].argv == (
        "kubectl", "rollout", "restart", "deployment/checkout-api", "--namespace", "payments"
    )
    calls: list[str] = []

    class Result:
        def to_dict(self):
            return {"status": "succeeded", "stdout": "", "stderr": "", "exit_code": 0, "truncated": False, "error_code": None, "error_message": None}

    monkeypatch.setattr(command_worker, "execute_command_envelope", lambda envelope, **_kwargs: calls.append(envelope.command_id) or Result())
    result = command_worker.execute_restart_command(
        command, connector_id="connector-prod", cluster_id="cluster-prod", allowed_namespaces={"payments"}
    )
    journal = ConnectorCommandJournal(tmp_path / "connector.db")
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
    command = {
        "id": "command-mutation", "cluster_id": "cluster-prod", "namespace": "payments",
        "action": action,
        "parameters": {"resource_kind": "Deployment", "deployment_name": "checkout-api", **parameters},
        "rollback_plan": None,
        "execution_grant_id": "grant-1", "execution_grant_expires_at": time.time() + 60,
        "action_hash": "a" * 64, "status": "leased", "attempt_count": 0,
        "lease_id": "lease-1", "lease_expires_at": 200, "created_at": 100, "result": None,
    }

    envelopes = command_worker.build_mutation_envelopes(command)

    assert envelopes[1].argv == expected_argv
    with pytest.raises(ValueError, match="unsupported mutation action"):
        command_worker.build_mutation_envelopes({**command, "action": "shell"})
    if action == "scale_deployment":
        with pytest.raises(ValueError, match="replica bounds"):
            command_worker.build_mutation_envelopes({
                **command,
                "parameters": {**command["parameters"], "target_replicas": 21},
            })


def test_post_check_runs_only_frozen_conditional_rollback_when_assumptions_match(monkeypatch) -> None:
    command = {
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
        "execution_grant_id": "grant-1", "execution_grant_expires_at": time.time() + 60,
        "action_hash": "a" * 64, "status": "leased", "attempt_count": 0,
        "lease_id": "lease-1", "lease_expires_at": 200, "created_at": 100, "result": None,
    }
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

    result = command_worker.execute_mutation_command(
        command, connector_id="connector-prod", cluster_id="cluster-prod", allowed_namespaces={"payments"}
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
        command, connector_id="connector-prod", cluster_id="cluster-prod", allowed_namespaces={"payments"}
    )
    assert changed["error_code"] == "rollback_required"


def test_revision_rollback_requires_the_exact_revision_after_post_check(monkeypatch) -> None:
    command = {
        "id": "command-rollback", "cluster_id": "cluster-prod", "namespace": "payments",
        "action": "rollback_deployment",
        "parameters": {"resource_kind": "Deployment", "deployment_name": "checkout-api", "target_revision": 41},
        "rollback_plan": None,
        "execution_grant_id": "grant-1", "execution_grant_expires_at": time.time() + 60,
        "action_hash": "a" * 64, "status": "leased", "attempt_count": 0,
        "lease_id": "lease-1", "lease_expires_at": 200, "created_at": 100, "result": None,
    }
    outputs = iter([
        "revision 41 exists", "", '{"metadata":{"annotations":{"deployment.kubernetes.io/revision":"42"}}}',
    ])

    class Result:
        def to_dict(self):
            return {
                "status": "succeeded", "stdout": next(outputs), "stderr": "", "exit_code": 0,
                "truncated": False, "error_code": None, "error_message": None,
            }

    monkeypatch.setattr(command_worker, "execute_command_envelope", lambda _envelope, **_kwargs: Result())

    result = command_worker.execute_mutation_command(
        command, connector_id="connector-prod", cluster_id="cluster-prod", allowed_namespaces={"payments"}
    )

    assert result["error_code"] == "rollback_required"
