from pathlib import Path
import hashlib
import json
import sqlite3

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
    evidenced = ConnectorCommandJournal(path).unreported_results_with_evidence()

    assert recovered == [(command, {"status": "succeeded", "stdout": '{"items":[]}'})]
    assert evidenced[0][2] == {
        "state": "terminal", "command_id": "command-1",
        "result_sha256": hashlib.sha256(
            b'{"status":"succeeded","stdout":"{\\"items\\":[]}"}'
        ).hexdigest(),
        "recorded_at": evidenced[0][2]["recorded_at"],
    }


def test_connector_journal_retries_started_reconciliation_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "connector.db"
    command = {
        "id": "command-reconcile", "cluster_id": "cluster-prod", "namespace": "payments",
        "action": "reconcile_kubernetes_change", "parameters": {"change": {}},
        "lease_id": "lease-1",
    }
    journal = ConnectorCommandJournal(path)
    assert journal.accept(command) == "accepted"
    journal.started("command-reconcile")

    restarted = ConnectorCommandJournal(path)
    assert restarted.accept({**command, "lease_id": "lease-2"}) == "accepted"
    restarted.started("command-reconcile")
    restarted.terminal("command-reconcile", {"status": "succeeded"})
    assert restarted.unreported_result("command-reconcile") == {"status": "succeeded"}


def test_connector_journal_removes_ciphertext_after_terminal_result(tmp_path: Path) -> None:
    path = tmp_path / "connector.db"
    command = {
        "id": "command-sensitive", "cluster_id": "cluster-prod", "namespace": "payments",
        "action": "execute_kubernetes_change", "execution_grant_id": "grant-1",
        "lease_id": "lease-1",
        "parameters": {
            "change": {"target": {"kind": "Secret", "name": "api-key"}},
            "secure_inputs": [{
                "id": "opaque-1", "key_name": "api.token", "placeholder": "opaque",
                "sha256": "a" * 64, "key_fingerprint": "b" * 64,
                "nonce": "nonce-value", "ciphertext": "ciphertext-value",
            }],
        },
    }
    journal = ConnectorCommandJournal(path)
    journal.accept(command)
    journal.started("command-sensitive")
    journal.terminal("command-sensitive", {"status": "succeeded"})

    recovered = journal.unreported_results()[0][0]
    assert recovered["parameters"]["secure_inputs"] == [{  # type: ignore[index]
        "key_name": "api.token", "sha256": "a" * 64,
    }]
    raw = path.read_bytes()
    assert b"ciphertext-value" not in raw and b"nonce-value" not in raw


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
    result_payloads: list[dict[str, object]] = []

    def post(_url, path, payload, _credential, **_kwargs):
        calls.append(path)
        if path.endswith("/poll"):
            return 200, {"command": command}
        if path.endswith("/result"):
            result_payloads.append(payload)
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
    assert result_payloads[0]["journal_evidence"]["state"] == "terminal"  # type: ignore[index]


def test_worker_redacts_sensitive_command_when_gateway_does_not_acknowledge_start(
    tmp_path: Path, monkeypatch,
) -> None:
    path = tmp_path / "connector.db"
    command = {
        "id": "command-sensitive", "cluster_id": "cluster-prod", "namespace": "payments",
        "action": "execute_kubernetes_change", "execution_grant_id": "grant-1",
        "lease_id": "lease-1",
        "parameters": {
            "change": {"target": {"kind": "Secret", "name": "api-key"}},
            "secure_inputs": [{
                "id": "opaque-1", "key_name": "api.token", "placeholder": "opaque",
                "sha256": "a" * 64, "key_fingerprint": "b" * 64,
                "nonce": "nonce-value", "ciphertext": "ciphertext-value",
            }],
        },
    }
    calls: list[str] = []

    def post(_url, request_path, _payload, _credential, **_kwargs):
        calls.append(request_path)
        if request_path.endswith("/poll"):
            return 200, {"command": command}
        return 400, {"error": {"code": "command_lease_expired"}}

    monkeypatch.setattr(command_worker, "_post_json", post)
    journal = ConnectorCommandJournal(path)

    assert not command_worker.run_command_cycle(
        "https://gateway.example",
        connector_id="connector-prod",
        cluster_id="cluster-prod",
        credential="credential",
        allowed_namespaces={"payments"},
        journal=journal,
        wait_seconds=0,
        change_executor=lambda *_args, **_kwargs: pytest.fail("change must not execute"),
    )
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT state, command_json FROM command_journal WHERE command_id = 'command-sensitive'"
        ).fetchone()
    assert row is not None and row[0] == "accepted"
    assert json.loads(row[1])["parameters"]["secure_inputs"] == [{
        "key_name": "api.token", "sha256": "a" * 64,
    }]
    raw = path.read_bytes()
    assert b"ciphertext-value" not in raw and b"nonce-value" not in raw
    assert calls == [
        "/api/v1/connectors/commands/poll",
        "/api/v1/connectors/commands/command-sensitive/start",
    ]
