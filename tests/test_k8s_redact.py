"""测试 Kubernetes 输出脱敏。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


def _load_module():
    """按文件路径加载模块，并补齐本仓库依赖路径。"""
    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root))

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
async def test_secret_json_data_is_redacted() -> None:
    module = _load_module()
    payload = {
        "apiVersion": "v1",
        "kind": "Secret",
        "data": {"username": "YWRtaW4=", "password": "c2VjcmV0"},
    }

    redacted = await module.redact_k8s_output(
        json.dumps(payload),
        "kubectl get secret demo -o json",
    )

    assert json.loads(redacted)["data"] == {
        "username": "[REDACTED]",
        "password": "[REDACTED]",
    }


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


@pytest.mark.asyncio
async def test_pod_json_service_account_token_projection_remains_valid_json() -> None:
    module = _load_module()
    payload = {
        "apiVersion": "v1",
        "kind": "List",
        "items": [{
            "apiVersion": "v1",
            "kind": "Pod",
            "spec": {
                "volumes": [{
                    "projected": {
                        "sources": [{
                            "serviceAccountToken": {
                                "expirationSeconds": 3607,
                                "path": "token",
                            }
                        }]
                    }
                }]
            },
        }],
    }
    output = json.dumps(payload)

    redacted = await module.redact_k8s_output(output, "kubectl get pods -o json")

    assert json.loads(redacted) == payload


@pytest.mark.asyncio
async def test_json_credentials_and_secret_references_are_structurally_redacted() -> None:
    module = _load_module()
    payload = {
        "password": 12345,
        "token": {"value": "nested-secret"},
        "env": [
            {"name": "DB_PASSWORD", "value": "plain-secret"},
            {"name": "LOG_LEVEL", "value": "info"},
        ],
        "secretKeyRef": {"name": "payment-db", "key": "password"},
    }

    redacted = json.loads(
        await module.redact_k8s_output(json.dumps(payload), "kubectl get pod api -o json")
    )

    assert redacted["password"] == "[REDACTED]"
    assert redacted["token"] == {"value": "[REDACTED]"}
    assert redacted["env"] == [
        {"name": "DB_PASSWORD", "value": "[REDACTED]"},
        {"name": "LOG_LEVEL", "value": "info"},
    ]
    assert redacted["secretKeyRef"] == {
        "name": "[REDACTED]",
        "key": "[REDACTED]",
    }


@pytest.mark.asyncio
async def test_config_map_json_data_is_not_over_redacted() -> None:
    module = _load_module()
    payload = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "data": {"log_level": "debug"},
    }

    redacted = await module.redact_k8s_output(
        json.dumps(payload),
        "kubectl get configmap runtime -o json",
    )

    assert json.loads(redacted) == payload
