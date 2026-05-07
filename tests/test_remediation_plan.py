"""Remediation action schema tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_module():
    module_path = Path(__file__).resolve().parents[1] / "toolsets" / "remediation_plan.py"
    spec = importlib.util.spec_from_file_location("test_remediation_plan_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_scale_suggestion_builds_structured_action() -> None:
    module = _load_module()

    result = module.build_remediation_context(
        "扩容 deployment/nginx 到 3 副本",
        incident_id="inc-1",
        alertname="KubeDeploymentReplicasMismatch",
        cluster="prod-a",
        namespace="default",
    )

    assert result["executable"] is True
    assert result["action_signature"] == "scale_deployment:prod-a:default:deployment/nginx:replicas=3"
    assert result["remediation_action"] == {
        "action_schema_version": "remediation.action.v1",
        "action_signature": "scale_deployment:prod-a:default:deployment/nginx:replicas=3",
        "action_type": "scale_deployment",
        "cluster": "prod-a",
        "namespace": "default",
        "resource_kind": "deployment",
        "resource_name": "nginx",
        "parameters": {"replicas": 3},
        "source": {
            "incident_id": "inc-1",
            "alertname": "KubeDeploymentReplicasMismatch",
            "analysis_action": "扩容 deployment/nginx 到 3 副本",
        },
        "risk": {"risk_level": "low", "operation_type": "k8s_write"},
    }


def test_restart_suggestion_builds_structured_action() -> None:
    module = _load_module()

    result = module.build_remediation_context(
        "重启 deployment/nginx",
        incident_id="inc-1",
        alertname="PodCrashLooping",
        cluster="prod-a",
        namespace="default",
    )

    assert result["executable"] is True
    assert result["action_signature"] == "restart_deployment:prod-a:default:deployment/nginx"
    assert result["remediation_action"]["parameters"] == {"strategy": "rollout_restart"}
    assert result["remediation_action"]["risk"] == {"risk_level": "low", "operation_type": "k8s_write"}


def test_unknown_suggestion_is_not_executable() -> None:
    module = _load_module()

    result = module.build_remediation_context(
        "检查最近 15 分钟的应用启动失败日志",
        incident_id="inc-1",
        alertname="PodCrashLooping",
        cluster="prod-a",
        namespace="default",
    )

    assert result["executable"] is False
    assert result["action_signature"] == "non_executable:prod-a:default:检查最近 15 分钟的应用启动失败日志"
    assert "remediation_action" not in result


def test_invalid_replicas_are_not_structured() -> None:
    module = _load_module()

    result = module.build_remediation_context(
        "扩容 deployment/nginx 到 21 副本",
        incident_id="inc-1",
        alertname="KubeDeploymentReplicasMismatch",
        cluster="prod-a",
        namespace="default",
    )

    assert result["executable"] is False
    assert result["non_executable_reason"] == "invalid_replicas"
    assert "remediation_action" not in result


def test_action_signature_is_stable() -> None:
    module = _load_module()
    kwargs = {
        "incident_id": "inc-1",
        "alertname": "PodCrashLooping",
        "cluster": "prod-a",
        "namespace": "default",
    }

    first = module.build_remediation_context("重启 deployment/nginx", **kwargs)
    second = module.build_remediation_context("重启 deployment/nginx", **kwargs)

    assert first["action_signature"] == second["action_signature"]
    assert first["remediation_action"] == second["remediation_action"]

