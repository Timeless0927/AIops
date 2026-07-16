from __future__ import annotations

import pytest

from apps.aiops_k8s_gateway import connector_command_http, connector_diagnosis_reads


class Handler:
    command = "POST"
    headers = {"Authorization": "Bearer projected-token"}

    def __init__(self) -> None:
        self.response = None

    def read_json_body(self):
        return {
            "cluster_id": "pilot-cluster",
            "namespace": "aiops-verification",
            "parameters": {
                "resource_kind": "pods",
                "selector": "app.kubernetes.io/name=verification-api",
                "output": "json",
            },
            "reason": "collect live Kubernetes evidence",
        }

    def write_json(self, status, payload):
        self.response = (status, payload)


class Commands:
    def queue_read(self, **kwargs):
        self.queued = kwargs
        return {"id": "command-read-1", "status": "queued"}

    def get(self, command_id):
        assert command_id == "command-read-1"
        return {
            "id": command_id,
            "cluster_id": "pilot-cluster",
            "namespace": "aiops-verification",
            "status": "succeeded",
            "result": {
                "status": "succeeded",
                "stdout": '{"apiVersion":"v1","kind":"PodList","items":[{"metadata":{"name":"verification-api"}}]}',
                "stderr": "",
                "exit_code": 0,
                "truncated": False,
                "error_code": None,
                "error_message": None,
            },
        }


class TerminalCommands(Commands):
    def __init__(self, status, result) -> None:
        self.status = status
        self.result = result

    def get(self, command_id):
        command = super().get(command_id)
        command["status"] = self.status
        command["result"] = self.result
        return command


class PendingCommands(Commands):
    def get(self, command_id):
        assert command_id == "command-read-1"
        return {"id": command_id, "status": "queued"}


def test_diagnosis_internal_read_uses_connector_command_and_returns_evidence(monkeypatch) -> None:
    monkeypatch.setattr(
        connector_command_http,
        "enforce_internal_auth",
        lambda *_args, **_kwargs: "system:serviceaccount:aiops-system:aiops-diagnosis",
        raising=False,
    )
    handler = Handler()
    commands = Commands()

    handled = connector_command_http.dispatch(
        handler,
        "/api/v1/internal/diagnosis/k8s-read",
        commands,
        identity=None,
        authorize_admin=None,
        request_id_for=lambda _handler: "request-read-1",
        extract_bearer=None,
        error_payload=lambda code, message, request_id: {
            "request_id": request_id,
            "error": {"code": code, "message": message},
        },
    )

    assert handled is True
    status, payload = handler.response
    assert status == 200
    assert payload["status"] == "succeeded"
    assert payload["data"]["items"][0]["metadata"]["name"] == "verification-api"
    assert payload["evidence_refs"][0]["ref_id"] == "connector-command:command-read-1"
    assert commands.queued["actor_id"] == "system:serviceaccount:aiops-system:aiops-diagnosis"


@pytest.mark.parametrize(
    ("status", "result", "error_code"),
    [
        ("succeeded", {"stdout": "{}", "exit_code": 0, "truncated": True}, "truncated_output"),
        ("succeeded", {"stdout": "not-json", "exit_code": 0, "truncated": False}, "invalid_json"),
        ("failed", {"error_code": "backend_unavailable", "error_message": "x" * 1000}, "backend_unavailable"),
    ],
)
def test_diagnosis_internal_read_returns_bounded_public_failures(
    monkeypatch, status, result, error_code
) -> None:
    monkeypatch.setattr(
        connector_command_http,
        "enforce_internal_auth",
        lambda *_args, **_kwargs: "system:serviceaccount:aiops-system:aiops-diagnosis",
    )
    handler = Handler()

    connector_command_http.dispatch(
        handler,
        "/api/v1/internal/diagnosis/k8s-read",
        TerminalCommands(status, result),
        identity=None,
        authorize_admin=None,
        request_id_for=lambda _handler: "request-read-1",
        extract_bearer=None,
        error_payload=lambda code, message, request_id: {"error": {"code": code}},
    )

    http_status, payload = handler.response
    assert http_status == 200
    assert payload["status"] == "failed"
    assert payload["data"] == {}
    assert payload["evidence_refs"] == []
    assert payload["audit"]["error_code"] == error_code
    assert len(payload["summary"]) <= 512


def test_diagnosis_internal_read_returns_bounded_timeout(monkeypatch) -> None:
    monotonic = iter((100.0, 116.0))
    monkeypatch.setattr(connector_diagnosis_reads.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(connector_diagnosis_reads.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        connector_command_http,
        "enforce_internal_auth",
        lambda *_args, **_kwargs: "system:serviceaccount:aiops-system:aiops-diagnosis",
    )
    handler = Handler()

    connector_command_http.dispatch(
        handler,
        "/api/v1/internal/diagnosis/k8s-read",
        PendingCommands(),
        identity=None,
        authorize_admin=None,
        request_id_for=lambda _handler: "request-read-timeout",
        extract_bearer=None,
        error_payload=lambda code, message, request_id: {"error": {"code": code}},
    )

    status, payload = handler.response
    assert status == 200
    assert payload["status"] == "failed"
    assert payload["summary"] == "Connector read timed out"
    assert payload["audit"]["error_code"] == "read_timeout"
