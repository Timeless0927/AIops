from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_registry():
    hermes_root = Path(__file__).resolve().parents[1] / "hermes-agent"
    if str(hermes_root) not in sys.path:
        sys.path.insert(0, str(hermes_root))
    from tools.registry import registry

    return registry


def _load_module():
    module_name = "test_proactive_risk_scan_module"
    if module_name in sys.modules:
        return sys.modules[module_name]

    module_path = Path(__file__).resolve().parents[1] / "toolsets" / "proactive_risk_scan.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_scan_returns_low_noise_empty_result(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    module = _load_module()
    calls: list[str] = []
    baseline = {"repeat_incident_count": 0, "repeat_incident_rate": 0.0}

    async def _k8s_read(command: str, context: str | None = None):
        del context
        calls.append(command)
        outputs = {
            "kubectl get pods -A": {"ok": True, "stdout": "NAMESPACE NAME READY STATUS RESTARTS AGE\ndefault api-1 1/1 Running 0 10m\n"},
            "kubectl get deploy -A": {"ok": True, "stdout": "NAMESPACE NAME READY UP-TO-DATE AVAILABLE AGE\ndefault api 3/3 3 3 10m\n"},
            "kubectl get nodes": {"ok": True, "stdout": "NAME STATUS ROLES AGE VERSION\nnode-a Ready worker 10d v1.30.0\n"},
        }
        return outputs[command]

    async def _compute_metrics(days: int = 7):
        assert days == 7
        return baseline

    monkeypatch.setattr(module.k8s_read_tool, "k8s_read", _k8s_read)
    monkeypatch.setattr(module.sre_metrics, "compute_metrics", _compute_metrics)

    result = await module.sre_proactive_risk_scan()

    assert result["ok"] is True
    assert calls == [
        "kubectl get pods -A",
        "kubectl get deploy -A",
        "kubectl get nodes",
    ]
    assert result["cluster_risk_baseline"] == baseline
    assert result["risks"] == []
    assert "未发现高重启 Pod、Unready workload 或 Node Ready 风险" in result["summary"]


@pytest.mark.asyncio
async def test_scan_detects_high_restart_pod(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    module = _load_module()

    async def _k8s_read(command: str, context: str | None = None):
        del context
        outputs = {
            "kubectl get pods -A": {
                "ok": True,
                "stdout": (
                    "NAMESPACE NAME READY STATUS RESTARTS AGE\n"
                    "default api-7d8c9f6c6f-x2m5q 1/1 Running 12 30m\n"
                ),
            },
            "kubectl get deploy -A": {"ok": True, "stdout": "NAMESPACE NAME READY UP-TO-DATE AVAILABLE AGE\ndefault api 3/3 3 3 10m\n"},
            "kubectl get nodes": {"ok": True, "stdout": "NAME STATUS ROLES AGE VERSION\nnode-a Ready worker 10d v1.30.0\n"},
        }
        return outputs[command]

    async def _compute_metrics(days: int = 7):
        return {"repeat_incident_count": 1, "repeat_incident_rate": 0.2}

    monkeypatch.setattr(module.k8s_read_tool, "k8s_read", _k8s_read)
    monkeypatch.setattr(module.sre_metrics, "compute_metrics", _compute_metrics)

    result = await module.sre_proactive_risk_scan()

    assert len(result["risks"]) == 1
    risk = result["risks"][0]
    assert risk["risk_type"] == "high_restart_pod"
    assert risk["scope"] == "workload"
    assert risk["resource_ref"] == "default/pod/api-7d8c9f6c6f-x2m5q"
    assert risk["severity"] == "warning"
    assert "restart count is high (12)" in risk["summary"]


def test_load_module_does_not_register_tool_in_global_registry() -> None:
    registry = _load_registry()

    first = _load_module()
    first_entry = registry.get_entry("sre_proactive_risk_scan")
    second = _load_module()
    second_entry = registry.get_entry("sre_proactive_risk_scan")

    assert first is second
    assert first_entry is second_entry
