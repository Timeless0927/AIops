from pathlib import Path

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
