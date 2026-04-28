"""主动风险扫描工具。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict


def _load_tool_module(module_filename: str, module_name: str):
    """按文件路径加载同目录工具模块。"""
    if module_name in sys.modules:
        return sys.modules[module_name]

    module_path = Path(__file__).resolve().parent / module_filename
    toolsets_root = str(module_path.parent)
    if toolsets_root not in sys.path:
        sys.path.insert(0, toolsets_root)
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块: {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _ensure_registry_import() -> None:
    """确保可以导入 Hermes 的工具注册器。"""
    hermes_root = Path(__file__).resolve().parents[1] / "hermes-agent"
    if str(hermes_root) not in sys.path:
        sys.path.insert(0, str(hermes_root))


_ensure_registry_import()

from tools.registry import registry  # noqa: E402


k8s_read_tool = _load_tool_module("k8s_read.py", "proactive_risk_scan_k8s_read")
sre_metrics = _load_tool_module("sre_metrics.py", "proactive_risk_scan_sre_metrics")


SRE_PROACTIVE_RISK_SCAN_SCHEMA = {
    "name": "sre_proactive_risk_scan",
    "description": "执行单集群主动风险扫描，输出高重启 Pod、Unready workload 与 Node 风险摘要。",
    "parameters": {
        "type": "object",
        "properties": {
            "days": {"type": "integer", "description": "历史重复 incident 基线统计周期，默认 7 天"},
        },
    },
}


def _parse_pod_rows(stdout: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in stdout.splitlines()[1:]:
        columns = line.split()
        if len(columns) < 6:
            continue
        namespace, name, ready, status, restarts, age = columns[:6]
        rows.append(
            {
                "namespace": namespace,
                "name": name,
                "ready": ready,
                "status": status,
                "restarts": restarts,
                "age": age,
            }
        )
    return rows


def _detect_high_restart_pod_risks(pods_stdout: str) -> list[dict[str, Any]]:
    risky_statuses = {"Running", "CrashLoopBackOff", "Error"}
    risks: list[dict[str, Any]] = []

    for pod in _parse_pod_rows(pods_stdout):
        status = pod["status"]
        if status not in risky_statuses:
            continue
        try:
            restarts = int(pod["restarts"])
        except ValueError:
            continue
        if restarts < 5:
            continue

        namespace = pod["namespace"]
        name = pod["name"]
        risks.append(
            {
                "risk_type": "high_restart_pod",
                "severity": "warning",
                "scope": "workload",
                "resource_ref": f"{namespace}/pod/{name}",
                "summary": f"Pod {name} restart count is high ({restarts})",
                "supporting_evidence": [
                    f"kubectl get pods -A shows STATUS={status}",
                    f"kubectl get pods -A shows RESTARTS={restarts}",
                ],
                "historical_context": {},
                "recommended_actions": [
                    "检查该 Pod 最近 15 分钟日志",
                    "核对该工作负载最近变更与资源配置",
                ],
                "confidence": 0.7,
            }
        )

    return risks


async def sre_proactive_risk_scan(days: int = 7) -> dict[str, Any]:
    """执行主动风险扫描，当前仅返回无风险的低噪声结果。"""
    pods_result = await k8s_read_tool.k8s_read("kubectl get pods -A")
    await k8s_read_tool.k8s_read("kubectl get deploy -A")
    await k8s_read_tool.k8s_read("kubectl get nodes")
    baseline = await sre_metrics.compute_metrics(days=days)
    risks = _detect_high_restart_pod_risks(str(pods_result.get("stdout", "")))

    return {
        "ok": True,
        "cluster_risk_baseline": baseline,
        "risks": risks,
        "summary": "未发现高重启 Pod、Unready workload 或 Node Ready 风险。" if not risks else f"发现 {len(risks)} 个主动风险。",
    }


async def _tool_sre_proactive_risk_scan(args: Dict[str, Any], **_: Any) -> str:
    """工具入口：执行主动风险扫描。"""
    result = await sre_proactive_risk_scan(days=int(args.get("days", 7) or 7))
    return json.dumps(result, ensure_ascii=False)


registry.register(
    name="sre_proactive_risk_scan",
    toolset="sre",
    schema=SRE_PROACTIVE_RISK_SCAN_SCHEMA,
    handler=_tool_sre_proactive_risk_scan,
    is_async=True,
)
