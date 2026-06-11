"""AIO-57 CommandTask, Grant, and Connector envelope skeleton tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import subprocess
import threading
from http.server import ThreadingHTTPServer
from unittest.mock import patch
import urllib.request

import pytest

from aiops.domain import CommandTask, CommandTaskStatus, CommandTaskStore, Grant
from aiops.k8s import CommandEnvelope, ResultEnvelope
from apps.aiops_k8s_gateway import main as gateway_main
from apps.cluster_connector import main as connector_main
from apps.cluster_connector.stream_client import ConnectorRegistration
from apps.cluster_connector.kubectl_executor import validate_command_envelope
from apps.cluster_connector.kubectl_executor import execute_command_envelope


def _command_envelope(**overrides: object) -> CommandEnvelope:
    payload = {
        "envelope_version": "v1",
        "task_id": "task-1",
        "command_id": "cmd-1",
        "cluster_id": "cluster-a",
        "namespace": "default",
        "action_type": "read",
        "argv": ("kubectl", "get", "pods", "-n", "default"),
        "timeout_seconds": 30,
        "output_limit_bytes": 4096,
        "grant_id": "grant-1",
    }
    payload.update(overrides)
    return CommandEnvelope(**payload)


def test_command_task_state_flow_records_events() -> None:
    store = CommandTaskStore()
    task = store.create(
        CommandTask.create(
            task_id="task-1",
            command_id="cmd-1",
            cluster_id="cluster-a",
            namespace="default",
            action_type="kubectl_get",
        )
    )

    assert task.status == CommandTaskStatus.CREATED

    for status in (
        CommandTaskStatus.PENDING_APPROVAL,
        CommandTaskStatus.APPROVED,
        CommandTaskStatus.QUEUED,
        CommandTaskStatus.DISPATCHED,
        CommandTaskStatus.RUNNING,
        CommandTaskStatus.SUCCEEDED,
    ):
        task = store.transition(task.task_id, status)

    assert task.status == CommandTaskStatus.SUCCEEDED
    assert [event.to_status for event in store.events_for("task-1")] == [
        CommandTaskStatus.CREATED,
        CommandTaskStatus.PENDING_APPROVAL,
        CommandTaskStatus.APPROVED,
        CommandTaskStatus.QUEUED,
        CommandTaskStatus.DISPATCHED,
        CommandTaskStatus.RUNNING,
        CommandTaskStatus.SUCCEEDED,
    ]


def test_command_task_rejects_invalid_transition() -> None:
    task = CommandTask.create(
        task_id="task-1",
        command_id="cmd-1",
        cluster_id="cluster-a",
        namespace="default",
        action_type="kubectl_get",
    )

    with pytest.raises(ValueError, match="invalid task transition"):
        task.transition_to(CommandTaskStatus.RUNNING)


def test_exact_task_replay_returns_existing_task() -> None:
    store = CommandTaskStore()
    original = store.create(
        CommandTask.create(
            task_id="task-1",
            command_id="cmd-1",
            cluster_id="cluster-a",
            namespace="default",
            action_type="kubectl_get",
        )
    )
    replay = store.create(
        CommandTask.create(
            task_id="task-1",
            command_id="cmd-1",
            cluster_id="cluster-a",
            namespace="default",
            action_type="kubectl_get",
        )
    )

    assert replay is original
    assert store.get("task-2") is None


def test_task_id_conflict_does_not_corrupt_command_id_mapping() -> None:
    store = CommandTaskStore()
    original = store.create(
        CommandTask.create(
            task_id="task-1",
            command_id="cmd-1",
            cluster_id="cluster-a",
            namespace="default",
            action_type="kubectl_get",
        )
    )

    with pytest.raises(ValueError, match="task_id already belongs"):
        store.create(
            CommandTask.create(
                task_id="task-1",
                command_id="cmd-2",
                cluster_id="cluster-a",
                namespace="default",
                action_type="kubectl_get",
            )
        )

    assert store.create(original) is original
    assert store.create(
        CommandTask.create(
            task_id="task-1",
            command_id="cmd-1",
            cluster_id="cluster-a",
            namespace="default",
            action_type="kubectl_get",
        )
    ) is original


def test_command_id_conflict_rejects_different_task_id() -> None:
    store = CommandTaskStore()
    store.create(
        CommandTask.create(
            task_id="task-1",
            command_id="cmd-1",
            cluster_id="cluster-a",
            namespace="default",
            action_type="kubectl_get",
        )
    )

    with pytest.raises(ValueError, match="command_id already belongs"):
        store.create(
            CommandTask.create(
                task_id="task-2",
                command_id="cmd-1",
                cluster_id="cluster-a",
                namespace="default",
                action_type="kubectl_get",
            )
        )


def test_grant_expires_and_allows_only_one_use() -> None:
    issued_at = datetime(2026, 6, 5, tzinfo=timezone.utc)
    grant = Grant(
        grant_id="grant-1",
        task_id="task-1",
        command_id="cmd-1",
        cluster_id="cluster-a",
        namespace="default",
        action="kubectl_get",
        risk_level="low",
        issued_by="approval-system",
        issued_at=issued_at,
        expires_at=issued_at + timedelta(minutes=5),
    )

    consumed = grant.consume(issued_at + timedelta(minutes=1))

    assert consumed.uses == 1
    assert consumed.can_use(issued_at + timedelta(minutes=2)) is False
    with pytest.raises(ValueError, match="expired or already consumed"):
        consumed.consume(issued_at + timedelta(minutes=2))
    assert grant.is_expired(issued_at + timedelta(minutes=6)) is True


def test_command_envelope_round_trip_and_invalid_payload() -> None:
    envelope = _command_envelope()

    assert CommandEnvelope.from_dict(envelope.to_dict()) == envelope
    with pytest.raises(ValueError, match="argv"):
        _command_envelope(argv=())


def test_result_envelope_round_trip_and_invalid_status() -> None:
    result = ResultEnvelope(
        envelope_version="v1",
        task_id="task-1",
        command_id="cmd-1",
        connector_id="connector-1",
        cluster_id="cluster-a",
        status="succeeded",
        stdout="ok",
        exit_code=0,
    )

    assert ResultEnvelope.from_dict(result.to_dict()) == result
    with pytest.raises(ValueError, match="invalid result status"):
        ResultEnvelope(
            envelope_version="v1",
            task_id="task-1",
            command_id="cmd-1",
            connector_id="connector-1",
            cluster_id="cluster-a",
            status="unknown",
        )


def test_connector_validation_accepts_scoped_kubectl_envelope() -> None:
    validate_command_envelope(
        _command_envelope(),
        connector_cluster_id="cluster-a",
        allowed_namespaces={"default"},
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"namespace": "kube-system"}, "namespace_out_of_scope"),
        ({"argv": ("bash", "-lc", "kubectl get pods")}, "command_rejected"),
        ({"argv": ("kubectl", "get", "pods", "--all-namespaces")}, "all-namespaces"),
        ({"argv": ("kubectl", "get", "pods", "-A")}, "all namespaces"),
        ({"argv": ("kubectl", "get", "pods", "-A=true")}, "all namespaces"),
        ({"argv": ("kubectl", "get", "pods", "-Atrue")}, "all namespaces"),
        ({"argv": ("kubectl", "get", "pods", "-n", "kube-system")}, "argv namespace"),
        ({"argv": ("kubectl", "get", "pods", "-n=kube-system")}, "argv namespace"),
        ({"argv": ("kubectl", "get", "pods", "-nkube-system")}, "argv namespace"),
        ({"argv": ("kubectl", "get", "pods", "--namespace", "kube-system")}, "argv namespace"),
        ({"argv": ("kubectl", "get", "pods", "--namespace=kube-system")}, "argv namespace"),
        ({"argv": ("kubectl", "get", "pods", "-n")}, "requires a value"),
        ({"argv": ("kubectl", "get", "--context=prod", "pods", "-n", "default")}, "context"),
        ({"argv": ("kubectl", "get", "--server=https://prod", "pods", "-n", "default")}, "server"),
        ({"argv": ("kubectl", "get", "--kubeconfig=/tmp/prod", "pods", "-n", "default")}, "kubeconfig"),
        ({"argv": ("kubectl", "get", "--as=cluster-admin", "pods", "-n", "default")}, "as"),
        ({"argv": ("kubectl", "get", "--request-timeout=0", "pods", "-n", "default")}, "request-timeout"),
        ({"argv": ("kubectl", "get", "--token=secret-token", "pods", "-n", "default")}, "token"),
        ({"argv": ("kubectl", "get", "--watch", "pods", "-n", "default")}, "watch"),
        ({"argv": ("kubectl", "get", "pods", "-n", "default", "--watch")}, "watch"),
        ({"argv": ("kubectl", "logs", "--follow", "pod/api", "-n", "default")}, "follow"),
        ({"argv": ("kubectl", "get", "-f", "manifest.yaml", "pods", "-n", "default")}, "flag -f"),
        ({"argv": ("kubectl", "get", "--filename=manifest.yaml", "pods", "-n", "default")}, "filename"),
        ({"action_type": "delete"}, "action_type must be read"),
        ({"risk_level": "high"}, "risk_level must be low"),
        ({"timeout_seconds": 601}, "timeout exceeds"),
        ({"output_limit_bytes": 1024 * 1024 + 1}, "output limit exceeds"),
        ({"grant_id": None}, "grant_ref is required"),
    ],
)
def test_connector_validation_rejects_invalid_envelope(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_command_envelope(
            _command_envelope(**overrides),
            connector_cluster_id="cluster-a",
            allowed_namespaces={"default"},
        )


@pytest.mark.parametrize(
    "argv",
    [
        ("kubectl", "get", "pods", "-n", "default"),
        ("kubectl", "get", "pods", "-n=default"),
        ("kubectl", "get", "pods", "-ndefault"),
        ("kubectl", "get", "pods", "--namespace", "default"),
        ("kubectl", "get", "pods", "--namespace=default"),
    ],
)
def test_connector_validation_accepts_matching_argv_namespace(argv: tuple[str, ...]) -> None:
    validate_command_envelope(
        _command_envelope(argv=argv),
        connector_cluster_id="cluster-a",
        allowed_namespaces={"default"},
    )


@pytest.mark.parametrize("risk_level", (None, "", "low"))
def test_connector_validation_accepts_empty_or_low_risk_level(risk_level: str | None) -> None:
    validate_command_envelope(
        _command_envelope(risk_level=risk_level),
        connector_cluster_id="cluster-a",
        allowed_namespaces={"default"},
    )


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (("kubectl", "delete", "pod", "api", "-n", "default"), "delete"),
        (("kubectl", "patch", "deployment", "api", "-n", "default"), "patch"),
        (("kubectl", "exec", "pod/api", "-n", "default"), "exec"),
        (("kubectl", "get", "secrets", "-n", "default"), "get secrets"),
        (("kubectl", "logs", "deployment/api", "-n", "default"), "logs is only allowed"),
        (("kubectl", "rollout", "restart", "deployment/api", "-n", "default"), "rollout"),
    ],
)
def test_connector_validation_rejects_mutating_or_non_allowlisted_reads(
    argv: tuple[str, ...],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_command_envelope(
            _command_envelope(argv=argv),
            connector_cluster_id="cluster-a",
            allowed_namespaces={"default"},
        )


@pytest.mark.parametrize(
    "argv",
    [
        ("kubectl", "get", "pods", "-n", "default"),
        ("kubectl", "get", "deploy", "-n", "default"),
        ("kubectl", "get", "events", "-n", "default"),
        ("kubectl", "get", "services", "-n", "default"),
        ("kubectl", "get", "configmaps", "-n", "default"),
        ("kubectl", "describe", "pod/api", "-n", "default"),
        ("kubectl", "describe", "deployment/api", "-n", "default"),
        ("kubectl", "logs", "pod/api", "-n", "default", "--tail", "20"),
        ("kubectl", "rollout", "history", "deployment/api", "-n", "default"),
        ("kubectl", "rollout", "status", "deployment/api", "-n", "default"),
    ],
)
def test_connector_validation_accepts_read_allowlist(argv: tuple[str, ...]) -> None:
    validate_command_envelope(
        _command_envelope(argv=argv),
        connector_cluster_id="cluster-a",
        allowed_namespaces={"default"},
    )


def test_execute_command_envelope_runs_argv_with_limits_and_redaction() -> None:
    calls: list[dict[str, object]] = []

    def fake_popen(argv, **kwargs):  # noqa: ANN001
        calls.append({"argv": argv, **kwargs})
        return subprocess.Popen(  # noqa: S603
            [
                "python3",
                "-c",
                "import sys; sys.stdout.write('NAME READY\\napi 1/1\\nDB_PASSWORD=super-secret-value\\n')",
            ],
            stdout=kwargs["stdout"],
            stderr=kwargs["stderr"],
        )

    result = execute_command_envelope(
        _command_envelope(output_limit_bytes=64),
        connector_id="connector-a",
        connector_cluster_id="cluster-a",
        allowed_namespaces={"default"},
        popen_factory=fake_popen,
    )

    assert result.status == "succeeded"
    assert result.exit_code == 0
    assert "DB_PASSWORD=[REDACTED]" in result.stdout
    assert "super-secret-value" not in result.stdout
    assert result.result_ref == "k8s-read://cluster-a/default/cmd-1"
    assert calls[0]["argv"] == ["kubectl", "get", "pods", "-n", "default"]
    assert calls[0]["stdout"] == subprocess.PIPE
    assert calls[0]["stderr"] == subprocess.PIPE
    assert calls[0]["text"] is False


def test_execute_command_envelope_returns_controlled_rejection() -> None:
    result = execute_command_envelope(
        _command_envelope(argv=("kubectl", "delete", "pod", "api", "-n", "default")),
        connector_id="connector-a",
        connector_cluster_id="cluster-a",
        allowed_namespaces={"default"},
    )

    assert result.status == "command_rejected"
    assert result.error_code == "command_rejected"
    assert "delete" in str(result.error_message)


def test_execute_command_envelope_enforces_output_limit() -> None:
    processes: list[subprocess.Popen[bytes]] = []

    def fake_popen(argv, **kwargs):  # noqa: ANN001, ARG001
        process = subprocess.Popen(  # noqa: S603
            ["python3", "-c", "import sys, time; sys.stdout.write('abcdef'); sys.stdout.flush(); time.sleep(5)"],
            stdout=kwargs["stdout"],
            stderr=kwargs["stderr"],
        )
        processes.append(process)
        return process

    result = execute_command_envelope(
        _command_envelope(output_limit_bytes=3),
        connector_id="connector-a",
        connector_cluster_id="cluster-a",
        allowed_namespaces={"default"},
        popen_factory=fake_popen,
    )

    assert result.status == "failed"
    assert result.stdout == "abc"
    assert result.truncated is True
    assert processes[0].returncode is not None


def test_execute_command_envelope_enforces_combined_stdout_stderr_output_limit() -> None:
    processes: list[subprocess.Popen[bytes]] = []

    def fake_popen(argv, **kwargs):  # noqa: ANN001, ARG001
        process = subprocess.Popen(  # noqa: S603
            [
                "python3",
                "-c",
                "import sys, time; sys.stdout.write('ab'); sys.stdout.flush(); "
                "sys.stderr.write('cdef'); sys.stderr.flush(); time.sleep(5)",
            ],
            stdout=kwargs["stdout"],
            stderr=kwargs["stderr"],
        )
        processes.append(process)
        return process

    result = execute_command_envelope(
        _command_envelope(output_limit_bytes=3),
        connector_id="connector-a",
        connector_cluster_id="cluster-a",
        allowed_namespaces={"default"},
        popen_factory=fake_popen,
    )

    assert result.status == "failed"
    assert len((result.stdout + result.stderr).encode("utf-8")) <= 3
    assert result.truncated is True
    assert processes[0].returncode is not None


def test_execute_command_envelope_returns_timeout_envelope() -> None:
    def fake_popen(argv, **kwargs):  # noqa: ANN001, ARG001
        return subprocess.Popen(  # noqa: S603
            ["python3", "-c", "import time; time.sleep(5)"],
            stdout=kwargs["stdout"],
            stderr=kwargs["stderr"],
        )

    result = execute_command_envelope(
        _command_envelope(timeout_seconds=1),
        connector_id="connector-a",
        connector_cluster_id="cluster-a",
        allowed_namespaces={"default"},
        popen_factory=fake_popen,
    )

    assert result.status == "failed"
    assert result.exit_code == -1
    assert result.error_code == "timeout"
    assert "timed out" in str(result.error_message)


def test_gateway_routes_k8s_read_to_registered_connector(monkeypatch) -> None:
    gateway_main._ROUTES.clear()
    connector_main.ConnectorHandler.registration = ConnectorRegistration(
        connector_id="connector-local",
        cluster_id="cluster-local",
        namespace_scope=("aiops-dev",),
        capabilities=("execute_read",),
    )
    connector_main.ConnectorHandler.gateway_url = ""
    connector_main.ConnectorHandler.registered_with_gateway = False

    connector_server = ThreadingHTTPServer(("127.0.0.1", 0), connector_main.ConnectorHandler)
    gateway_server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    connector_thread = threading.Thread(target=connector_server.serve_forever, daemon=True)
    gateway_thread = threading.Thread(target=gateway_server.serve_forever, daemon=True)
    connector_thread.start()
    gateway_thread.start()
    connector_url = f"http://127.0.0.1:{connector_server.server_address[1]}"
    gateway_url = f"http://127.0.0.1:{gateway_server.server_address[1]}"
    monkeypatch.setenv("AIOPS_CONNECTOR_URL", connector_url)
    real_popen = subprocess.Popen

    try:
        with patch(
            "apps.cluster_connector.kubectl_executor.subprocess.Popen",
            side_effect=lambda argv, **kwargs: real_popen(  # noqa: S603
                ["python3", "-c", "import sys; sys.stdout.write('NAME READY\\naiops-api 1/1\\n')"],
                stdout=kwargs["stdout"],
                stderr=kwargs["stderr"],
            ),
        ):
            registration_body = json.dumps(
                {
                    "connector_id": "connector-local",
                    "cluster_id": "cluster-local",
                    "namespace_scope": ["aiops-dev"],
                    "capabilities": ["execute_read"],
                }
            ).encode("utf-8")
            register_request = urllib.request.Request(
                f"{gateway_url}/connectors/register",
                data=registration_body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(register_request, timeout=3) as response:
                assert response.status == 201

            read_body = json.dumps(
                {
                    "cluster_id": "cluster-local",
                    "namespace": "aiops-dev",
                    "argv": ["kubectl", "get", "pods", "-n", "aiops-dev"],
                    "reason": "test gateway connector read loop",
                    "task_id": "task-http",
                    "command_id": "cmd-http",
                }
            ).encode("utf-8")
            read_request = urllib.request.Request(
                f"{gateway_url}/k8s/read",
                data=read_body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(read_request, timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))

        assert payload["status"] == "succeeded"
        assert payload["connector_id"] == "connector-local"
        assert payload["stdout"].startswith("NAME READY")
        assert payload["result_ref"] == "k8s-read://cluster-local/aiops-dev/cmd-http"
    finally:
        connector_server.shutdown()
        gateway_server.shutdown()
        connector_server.server_close()
        gateway_server.server_close()
        connector_thread.join(timeout=2)
        gateway_thread.join(timeout=2)
        gateway_main._ROUTES.clear()
