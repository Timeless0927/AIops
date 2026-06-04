"""Kubernetes 工具入口测试。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from toolsets.k8s_exec import k8s_exec
from toolsets.k8s_read import _normalize_command_tokens, k8s_read, run_k8s_read
from toolsets.k8s_write import execute_approved as execute_write_approved
from toolsets.k8s_write import k8s_write


def test_normalize_command_tokens_injects_context() -> None:
    tokens = _normalize_command_tokens("kubectl get pods -n default", "prod-cluster")
    assert tokens[:3] == ["kubectl", "--context", "prod-cluster"]


def test_k8s_read_rejects_write_command() -> None:
    result = asyncio.run(k8s_read("kubectl apply -f deploy.yaml"))
    assert result["ok"] is False
    assert "k8s_write" in result["error"]


def test_k8s_write_returns_standard_approval() -> None:
    result = asyncio.run(k8s_write("kubectl apply -f deploy.yaml", "prod"))
    assert result["ok"] is True
    assert result["requires_approval"] is True
    assert result["approval_level"] == "standard"
    assert result["context"] == "prod"


def test_k8s_write_marks_dangerous_delete() -> None:
    result = asyncio.run(k8s_write("kubectl delete deployment web", None))
    assert result["ok"] is True
    assert result["approval_level"] == "dangerous"


def test_k8s_exec_requires_can_approve() -> None:
    result = asyncio.run(k8s_exec("kubectl exec -it pod/nginx -- sh", "prod"))
    assert result["ok"] is True
    assert result["approval_level"] == "elevated"
    assert result["requires_can_approve"] is True


def test_execute_approved_write_redacts_and_extracts() -> None:
    with patch("toolsets.k8s_write._run_kubectl", new=AsyncMock(return_value={
        "ok": True,
        "exit_code": 0,
        "stdout": "DB_PASSWORD=secret",
        "stderr": "",
        "executed_command": ["kubectl", "apply", "-f", "deploy.yaml"],
    })), patch("toolsets.k8s_write.extract_if_needed", new=AsyncMock(return_value={
        "extracted": False,
        "data": "DB_PASSWORD=[REDACTED]",
        "line_count": 1,
    })):
        result = asyncio.run(execute_write_approved("kubectl apply -f deploy.yaml", None))

    assert result["ok"] is True
    assert "[REDACTED]" in result["stdout"]
    assert result["result"]["line_count"] == 1


def _base_run_read_args(**overrides):
    args = {
        "cluster_id": "qa-shanghai",
        "namespace": "payment",
        "argv": ["kubectl", "get", "pods", "-n", "payment", "-o", "wide"],
        "reason": "查看 Pod 状态",
        "task_id": "task-read-1",
        "command_id": "cmd-read-1",
        "actor_id": "agent:center",
    }
    args.update(overrides)
    return args


def _ok_execution(argv, timeout_seconds, output_limit_bytes):
    assert argv == ["kubectl", "get", "pods", "-n", "payment", "-o", "wide"]
    assert timeout_seconds == 15
    assert output_limit_bytes == 262144
    return {
        "ok": True,
        "exit_code": 0,
        "stdout": "NAME READY\napi 1/1\n",
        "stderr": "",
        "executed_command": argv,
        "truncated": False,
        "error_code": None,
    }


def test_run_k8s_read_allows_pods_and_returns_v1_envelope() -> None:
    calls: list[dict] = []

    async def _audit(**kwargs):
        calls.append(kwargs)
        return 42

    with patch("toolsets.k8s_read._run_kubectl_argv", new=AsyncMock(side_effect=_ok_execution)), \
            patch("toolsets.k8s_read.audit_log.record_audit", new=AsyncMock(side_effect=_audit)):
        result = asyncio.run(run_k8s_read(**_base_run_read_args()))

    assert result["envelope_version"] == "result.envelope.v1"
    assert result["status"] == "succeeded"
    assert result["stdout"] == "NAME READY\napi 1/1\n"
    assert result["error"] is None
    assert result["audit_ref"] == "42"
    assert calls[0]["tool_name"] == "run_k8s_read"
    assert calls[0]["cluster"] == "qa-shanghai"
    assert calls[0]["namespace"] == "payment"
    assert '"decision": "executed"' in calls[0]["result"]
    assert '"requires_approval": false' in calls[0]["result"]
    assert '"argv_digest": "sha256:' in calls[0]["result"]


def test_run_k8s_read_rejects_shell_string_argv() -> None:
    result = asyncio.run(run_k8s_read(**_base_run_read_args(argv="kubectl get pods -n payment")))

    assert result["status"] == "failed"
    assert result["error"]["code"] == "command_rejected"
    assert "argv 必须是数组" in result["error"]["message"]
    assert result["envelope_version"] == "result.envelope.v1"


def test_run_k8s_read_rejects_mutation_command_without_approval() -> None:
    result = asyncio.run(run_k8s_read(**_base_run_read_args(argv=["kubectl", "delete", "pod", "api", "-n", "payment"])))

    assert result["status"] == "failed"
    assert result["error"]["code"] == "command_rejected"
    assert "delete" in result["error"]["message"]


def test_run_k8s_read_rejects_secret_and_raw_reads() -> None:
    secret = asyncio.run(run_k8s_read(**_base_run_read_args(argv=["kubectl", "get", "secret", "db", "-n", "payment", "-o", "yaml"])))
    raw = asyncio.run(run_k8s_read(**_base_run_read_args(argv=["kubectl", "get", "--raw", "/api"])))
    multi_resource = asyncio.run(run_k8s_read(**_base_run_read_args(argv=["kubectl", "get", "pods,secrets", "-n", "payment"])))

    assert secret["status"] == "failed"
    assert "Secret" in secret["error"]["message"]
    assert raw["status"] == "failed"
    assert raw["error"]["code"] == "command_rejected"
    assert multi_resource["status"] == "failed"
    assert multi_resource["error"]["code"] == "command_rejected"


def test_run_k8s_read_rejects_unscoped_all_namespaces_and_namespace_mismatch() -> None:
    all_ns = asyncio.run(run_k8s_read(**_base_run_read_args(argv=["kubectl", "get", "pods", "--all-namespaces"])))
    mismatch = asyncio.run(run_k8s_read(**_base_run_read_args(argv=["kubectl", "get", "pods", "-n", "other"])))

    assert all_ns["error"]["code"] == "namespace_out_of_scope"
    assert mismatch["error"]["code"] == "namespace_out_of_scope"


def test_run_k8s_read_rejects_operator_without_read_permission() -> None:
    profile = {
        "name": "只读外用户",
        "namespaces": ["payment"],
        "allowed_tools": ["k8s_write"],
    }

    result = asyncio.run(run_k8s_read(**_base_run_read_args(operator_profile=profile)))

    assert result["status"] == "failed"
    assert result["error"]["code"] == "permission_denied"


def test_run_k8s_read_enforces_prod_configmap_and_logs_limits() -> None:
    configmap = asyncio.run(run_k8s_read(**_base_run_read_args(
        environment="prod",
        argv=["kubectl", "get", "configmaps", "-n", "payment", "-o", "yaml"],
    )))
    logs = asyncio.run(run_k8s_read(**_base_run_read_args(
        environment="prod",
        argv=["kubectl", "logs", "pod/api", "-n", "payment", "--tail", "5000", "--since", "10m"],
    )))

    assert configmap["status"] == "failed"
    assert "prod ConfigMap" in configmap["error"]["message"]
    assert logs["status"] == "failed"
    assert "--tail" in logs["error"]["message"]


def test_run_k8s_read_adds_prod_log_defaults() -> None:
    async def _execution(argv, *_args):
        assert argv[-4:] == ["--tail", "200", "--since", "30m"]
        return {
            "ok": True,
            "exit_code": 0,
            "stdout": "short logs",
            "stderr": "",
            "executed_command": argv,
            "truncated": False,
            "error_code": None,
        }

    with patch("toolsets.k8s_read._run_kubectl_argv", new=AsyncMock(side_effect=_execution)), \
            patch("toolsets.k8s_read.audit_log.record_audit", new=AsyncMock(return_value=7)):
        result = asyncio.run(run_k8s_read(**_base_run_read_args(
            environment="prod",
            argv=["kubectl", "logs", "pod/api", "-n", "payment"],
        )))

    assert result["status"] == "succeeded"


def test_run_k8s_read_reports_output_truncation() -> None:
    with patch("toolsets.k8s_read._run_kubectl_argv", new=AsyncMock(return_value={
        "ok": True,
        "exit_code": 0,
        "stdout": "abc",
        "stderr": "",
        "executed_command": ["kubectl", "get", "pods"],
        "truncated": True,
        "error_code": None,
    })), patch("toolsets.k8s_read.audit_log.record_audit", new=AsyncMock(return_value=8)):
        result = asyncio.run(run_k8s_read(**_base_run_read_args(output_limit_bytes=3)))

    assert result["status"] == "succeeded"
    assert result["truncated"] is True


def test_run_k8s_read_timeout_and_backend_failure_return_v1_envelope() -> None:
    timeout = {
        "ok": False,
        "exit_code": -1,
        "stdout": "",
        "stderr": "kubectl 执行超时（1s）",
        "executed_command": ["kubectl", "get", "pods"],
        "truncated": False,
        "error_code": "timeout",
    }
    backend_failure = {
        "ok": False,
        "exit_code": -1,
        "stdout": "",
        "stderr": "kubectl 启动失败",
        "executed_command": ["kubectl", "get", "pods"],
        "truncated": False,
        "error_code": "backend_unavailable",
    }

    with patch("toolsets.k8s_read._run_kubectl_argv", new=AsyncMock(return_value=timeout)), \
            patch("toolsets.k8s_read.audit_log.record_audit", new=AsyncMock(return_value=9)):
        timeout_result = asyncio.run(run_k8s_read(**_base_run_read_args(timeout_seconds=1)))

    with patch("toolsets.k8s_read._run_kubectl_argv", new=AsyncMock(return_value=backend_failure)), \
            patch("toolsets.k8s_read.audit_log.record_audit", new=AsyncMock(return_value=10)):
        backend_result = asyncio.run(run_k8s_read(**_base_run_read_args()))

    assert timeout_result["status"] == "failed"
    assert timeout_result["error"]["code"] == "timeout"
    assert timeout_result["envelope_version"] == "result.envelope.v1"
    assert backend_result["status"] == "failed"
    assert backend_result["error"]["code"] == "backend_unavailable"
