"""K04 Connector structured Kubernetes execution tests."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace
import sqlite3

import pytest

from apps.cluster_connector.kubernetes_change_adapter import execute_change_command
from apps.cluster_connector import command_worker
from apps.cluster_connector.command_worker import ConnectorCommandJournal


class NotFound(Exception):
    status = 404


class Rejected(Exception):
    status = 422


class FakeResources:
    def __init__(self, *, api_version: str = "apps/v1", kind: str = "Deployment") -> None:
        self.resource = SimpleNamespace(
            api_version=api_version, kind=kind, name=f"{kind.lower()}s", namespaced=True,
            verbs=("get", "create", "patch", "delete"),
        )

    def get(self, **_kwargs: object) -> SimpleNamespace:
        return self.resource


class FakeClient:
    def __init__(
        self,
        live: dict[str, object] | None,
        final: dict[str, object] | None,
        *,
        reject: bool = False,
    ) -> None:
        self.resources = FakeResources()
        self.live = deepcopy(live)
        self.final = deepcopy(final)
        self.reject = reject
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, _resource: object, **kwargs: object) -> dict[str, object]:
        self.calls.append(("get", kwargs))
        if self.live is None:
            raise NotFound()
        return deepcopy(self.live)

    def create(self, _resource: object, **kwargs: object) -> dict[str, object]:
        self.calls.append(("create", kwargs))
        if self.reject:
            raise Rejected()
        self.live = deepcopy(self.final)
        return deepcopy(self.final or {})

    def patch(self, _resource: object, **kwargs: object) -> dict[str, object]:
        self.calls.append(("patch", kwargs))
        if self.reject:
            raise Rejected()
        self.live = deepcopy(self.final)
        return deepcopy(self.final or {})

    def delete(self, _resource: object, **kwargs: object) -> dict[str, object]:
        self.calls.append(("delete", kwargs))
        if self.reject:
            raise Rejected()
        self.live = None
        return {"kind": "Status", "status": "Success"}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _live(*, replicas: int = 3, version: str = "41") -> dict[str, object]:
    return {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {
            "name": "checkout-api", "namespace": "payments",
            "uid": "uid-1", "resourceVersion": version,
        },
        "spec": {"replicas": replicas},
    }


def _patch_change() -> dict[str, object]:
    return {
        "target": {
            "api_version": "apps/v1", "kind": "Deployment", "namespace": "payments",
            "name": "checkout-api", "uid": "uid-1", "resource_version": "41",
        },
        "operation": "patch",
        "payload": [
            {"op": "test", "path": "/metadata/uid", "value": "uid-1"},
            {"op": "test", "path": "/metadata/resourceVersion", "value": "41"},
            {"op": "test", "path": "/spec/replicas", "value": 3},
            {"op": "replace", "path": "/spec/replicas", "value": 5},
        ],
        "post_checks": [
            {"type": "json_pointer", "path": "/spec/replicas", "operator": "eq", "value": 5},
        ],
    }


def _command(change: dict[str, object], *, expires_at: float = 160.0) -> dict[str, object]:
    digest = hashlib.sha256(_json(change).encode()).hexdigest()
    return {
        "id": "command-1", "cluster_id": "cluster-prod", "namespace": "payments",
        "action": "execute_kubernetes_change",
        "parameters": {
            "grant": {
                "id": "grant-1", "phase_id": "phase-1", "approval_id": "approval-1",
                "change_hash": digest, "issued_at": 100.0, "expires_at": expires_at,
                "execution_timeout_seconds": 300,
            },
            "change": change,
        },
        "execution_grant_id": "grant-1", "execution_grant_expires_at": expires_at,
        "action_hash": digest,
    }


def test_patch_revalidates_preconditions_executes_and_reports_frozen_post_check() -> None:
    final = _live(replicas=5, version="42")
    client = FakeClient(_live(), final)

    result = execute_change_command(
        _command(_patch_change()), connector_cluster_id="cluster-prod",
        allowed_namespaces={"*"}, now=110.0, client_factory=lambda: client,
    )

    assert result["status"] == "succeeded"
    assert [call[0] for call in client.calls] == ["get", "patch", "get"]
    assert client.calls[1][1]["content_type"] == "application/json-patch+json"
    assert "dry_run" not in client.calls[1][1]
    assert result["execution"]["post_checks"] == [{  # type: ignore[index]
        "type": "json_pointer", "status": "succeeded",
    }]


def test_stale_live_precondition_returns_zero_mutation() -> None:
    client = FakeClient(_live(version="42"), _live(replicas=5, version="43"))

    result = execute_change_command(
        _command(_patch_change()), connector_cluster_id="cluster-prod",
        allowed_namespaces={"payments"}, now=110.0, client_factory=lambda: client,
    )

    assert (result["status"], result["error_code"]) == ("rejected", "stale_change")
    assert [call[0] for call in client.calls] == ["get"]


@pytest.mark.parametrize("missing_path", ["/metadata/uid", "/spec/replicas"])
def test_incomplete_frozen_preconditions_return_zero_mutation(missing_path: str) -> None:
    change = _patch_change()
    change["payload"] = [
        item for item in change["payload"]  # type: ignore[union-attr]
        if item.get("path") != missing_path or item.get("op") != "test"
    ]
    client = FakeClient(_live(), _live(replicas=5, version="42"))

    result = execute_change_command(
        _command(change), connector_cluster_id="cluster-prod",
        allowed_namespaces={"*"}, now=110.0, client_factory=lambda: client,
    )

    assert (result["status"], result["error_code"]) == ("rejected", "execution_grant_invalid")
    assert [call[0] for call in client.calls] == ["get"]


@pytest.mark.parametrize("failure", ["grant", "hash", "expired"])
def test_grant_and_change_hash_are_rechecked_before_live_read(failure: str) -> None:
    command = _command(_patch_change(), expires_at=109.0 if failure == "expired" else 160.0)
    if failure == "grant":
        command["execution_grant_id"] = "another-grant"
    elif failure == "hash":
        command["action_hash"] = "0" * 64
    client = FakeClient(_live(), _live(replicas=5, version="42"))

    result = execute_change_command(
        command, connector_cluster_id="cluster-prod", allowed_namespaces={"*"},
        now=110.0, client_factory=lambda: client,
    )

    assert result["status"] == "rejected"
    assert result["error_code"] == "execution_grant_invalid"
    assert client.calls == []


def test_api_rejection_and_post_check_failure_are_distinct_trustworthy_outcomes() -> None:
    rejected_client = FakeClient(_live(), _live(replicas=5, version="42"), reject=True)
    rejected = execute_change_command(
        _command(_patch_change()), connector_cluster_id="cluster-prod",
        allowed_namespaces={"*"}, now=110.0, client_factory=lambda: rejected_client,
    )
    failed_check_client = FakeClient(_live(), _live(replicas=4, version="42"))
    failed_check = execute_change_command(
        _command(_patch_change()), connector_cluster_id="cluster-prod",
        allowed_namespaces={"*"}, now=110.0, client_factory=lambda: failed_check_client,
    )

    assert (rejected["status"], rejected["error_code"]) == ("failed", "kubernetes_api_rejected")
    assert (failed_check["status"], failed_check["error_code"]) == ("failed", "post_check_failed")


def test_post_check_polls_until_success_within_execution_deadline() -> None:
    client = FakeClient(_live(), _live(replicas=4, version="42"))
    observed = [110.0]

    def advance(seconds: float) -> None:
        observed[0] += seconds
        client.live = _live(replicas=5, version="43")

    result = execute_change_command(
        _command(_patch_change()), connector_cluster_id="cluster-prod",
        allowed_namespaces={"*"}, now=observed[0], client_factory=lambda: client,
        clock=lambda: observed[0], sleeper=advance,
    )

    assert result["status"] == "succeeded"
    assert [call[0] for call in client.calls] == ["get", "patch", "get", "get"]


def test_post_check_stops_at_execution_deadline() -> None:
    client = FakeClient(_live(), _live(replicas=4, version="42"))
    observed = [110.0]

    def expire(_seconds: float) -> None:
        observed[0] = 410.0

    result = execute_change_command(
        _command(_patch_change()), connector_cluster_id="cluster-prod",
        allowed_namespaces={"*"}, now=observed[0], client_factory=lambda: client,
        clock=lambda: observed[0], sleeper=expire,
    )

    assert (result["status"], result["error_code"]) == ("failed", "post_check_failed")


@pytest.mark.parametrize("slow_step", ["discovery", "pre_read"])
def test_execution_deadline_is_rechecked_before_mutation(slow_step: str) -> None:
    observed = [110.0]
    client = FakeClient(_live(), _live(replicas=5, version="42"))
    if slow_step == "discovery":
        original_discovery = client.resources.get

        def discover(**kwargs: object):
            observed[0] = 411.0
            return original_discovery(**kwargs)

        client.resources.get = discover
    else:
        original_get = client.get

        def read(resource: object, **kwargs: object):
            observed[0] = 411.0
            return original_get(resource, **kwargs)

        client.get = read

    result = execute_change_command(
        _command(_patch_change()), connector_cluster_id="cluster-prod",
        allowed_namespaces={"*"}, now=110.0, client_factory=lambda: client,
        clock=lambda: observed[0],
    )

    assert (result["status"], result["error_code"]) == ("failed", "execution_timeout")
    assert all(call[0] != "patch" for call in client.calls)


@pytest.mark.parametrize("operation", ["create", "delete"])
def test_create_and_delete_use_structured_api_calls(operation: str) -> None:
    if operation == "create":
        payload = {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "checkout-api", "namespace": "payments"},
            "spec": {"replicas": 1},
        }
        change = {
            "target": {
                "api_version": "apps/v1", "kind": "Deployment", "namespace": "payments",
                "name": "checkout-api", "uid": None, "resource_version": None,
            },
            "operation": "create", "payload": payload,
            "post_checks": [{"type": "exists"}],
        }
        client = FakeClient(None, _live(replicas=1, version="1"))
    else:
        change = {
            "target": {
                "api_version": "apps/v1", "kind": "Deployment", "namespace": "payments",
                "name": "checkout-api", "uid": "uid-1", "resource_version": "41",
            },
            "operation": "delete",
            "payload": {
                "apiVersion": "v1", "kind": "DeleteOptions", "propagationPolicy": "Foreground",
                "preconditions": {"uid": "uid-1", "resourceVersion": "41"},
            },
            "post_checks": [{"type": "absent"}],
        }
        client = FakeClient(_live(), None)

    result = execute_change_command(
        _command(change), connector_cluster_id="cluster-prod", allowed_namespaces={"*"},
        now=110.0, client_factory=lambda: client,
    )

    assert result["status"] == "succeeded"
    assert operation in [call[0] for call in client.calls]


def test_worker_journals_started_and_terminal_before_execution_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = _command(_patch_change())
    command.update({
        "status": "leased", "attempt_count": 0, "lease_id": "lease-1",
        "lease_expires_at": 150.0, "created_at": 100.0, "result": None,
    })
    duplicate = {**command, "id": "command-2", "lease_id": "lease-2"}
    polled = [command, duplicate, command]
    journal = ConnectorCommandJournal(tmp_path / "connector.db", clock=lambda: 110.0)
    calls: list[str] = []
    results: list[str] = []

    def state(command_id: str = "command-1") -> str:
        with sqlite3.connect(journal.db_path) as conn:
            return str(conn.execute(
                "SELECT state FROM command_journal WHERE command_id = ?", (command_id,),
            ).fetchone()[0])

    def post(_url: str, path: str, payload: dict[str, object], _credential: str, **_kwargs: object):
        calls.append(path)
        if path.endswith("/poll"):
            return 200, {"command": polled.pop(0)}
        if path.endswith("/result"):
            assert state(path.split("/")[-2]) == "terminal"
            results.append(str(payload["result"]["status"]))  # type: ignore[index]
        return 200, {}

    def execute(*_args: object, **_kwargs: object) -> dict[str, object]:
        calls.append("execute")
        assert state() == "started"
        return {"status": "succeeded", "execution": {"post_checks": []}}

    monkeypatch.setattr(command_worker, "_post_json", post)
    assert command_worker.run_command_cycle(
        "https://gateway.example", connector_id="connector-prod", cluster_id="cluster-prod",
        credential="credential", allowed_namespaces={"payments"}, journal=journal,
        wait_seconds=0, clock=lambda: 110.0, change_executor=execute,
    )
    assert calls == [
        "/api/v1/connectors/commands/poll",
        "/api/v1/connectors/commands/command-1/start",
        "execute",
        "/api/v1/connectors/commands/command-1/result",
    ]
    assert journal.unreported_results() == []
    assert command_worker.run_command_cycle(
        "https://gateway.example", connector_id="connector-prod", cluster_id="cluster-prod",
        credential="credential", allowed_namespaces={"payments"}, journal=journal,
        wait_seconds=0, clock=lambda: 110.0, change_executor=execute,
    )
    assert calls[-3:] == [
        "/api/v1/connectors/commands/poll",
        "/api/v1/connectors/commands/command-2/start",
        "/api/v1/connectors/commands/command-2/result",
    ]
    assert calls.count("execute") == 1
    assert results == ["succeeded", "rejected"]
    before_replay = list(calls)
    assert not command_worker.run_command_cycle(
        "https://gateway.example", connector_id="connector-prod", cluster_id="cluster-prod",
        credential="credential", allowed_namespaces={"payments"}, journal=journal,
        wait_seconds=0, clock=lambda: 110.0, change_executor=execute,
    )
    assert calls == before_replay + ["/api/v1/connectors/commands/poll"]
    assert calls.count("execute") == 1
