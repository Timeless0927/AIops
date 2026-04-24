"""Kubernetes 工具入口测试。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from toolsets.k8s_exec import k8s_exec
from toolsets.k8s_read import _normalize_command_tokens, k8s_read
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
