"""测试 Kubernetes 输出脱敏。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_module():
    """按文件路径加载模块，并补齐上游依赖路径。"""
    repo_root = Path(__file__).resolve().parents[1]
    hermes_root = repo_root / "hermes-agent"
    sys.path.insert(0, str(repo_root))
    sys.path.insert(0, str(hermes_root))

    module_path = repo_root / "toolsets" / "k8s_redact.py"
    spec = importlib.util.spec_from_file_location("test_k8s_redact_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_secret_yaml_data_is_redacted() -> None:
    """Secret YAML 中 data 字段值应被替换。"""
    module = _load_module()
    output = """apiVersion: v1
kind: Secret
metadata:
  name: demo
data:
  username: YWRtaW4=
  password: c2VjcmV0
type: Opaque
"""

    redacted = await module.redact_k8s_output(output, "kubectl get secret demo -o yaml")

    assert "username: [REDACTED]" in redacted
    assert "password: [REDACTED]" in redacted


@pytest.mark.asyncio
async def test_sensitive_env_assignment_is_redacted() -> None:
    """敏感环境变量值应被脱敏。"""
    module = _load_module()

    redacted = await module.redact_k8s_output("API_KEY=super-secret-value", "kubectl logs pod/demo")

    assert redacted == "API_KEY=[REDACTED]"


@pytest.mark.asyncio
async def test_secret_refs_in_describe_output_are_redacted() -> None:
    """describe 输出中的 secretRef / token / password 字段应被脱敏。"""
    module = _load_module()
    output = """Environment:
  DB_PASSWORD: super-secret
  TOKEN: bearer-token
  DB_USER:
    SecretKeyRef:
      Name: payment-db
      Key: username
"""

    redacted = await module.redact_k8s_output(output, "kubectl describe pod api -n payment")

    assert "DB_PASSWORD: [REDACTED]" in redacted
    assert "TOKEN: [REDACTED]" in redacted
    assert "Name: [REDACTED]" in redacted
    assert "Key: [REDACTED]" in redacted
    assert "super-secret" not in redacted
    assert "payment-db" not in redacted


@pytest.mark.asyncio
async def test_normal_output_is_not_over_redacted() -> None:
    """普通输出不应被误脱敏。"""
    module = _load_module()
    output = "pod/demo Running 3/3\nservice/api ClusterIP"

    redacted = await module.redact_k8s_output(output, "kubectl get pods")

    assert redacted == output
