"""Alertmanager Webhook 处理 Hook。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
from typing import Any, Dict, List
import sys
import time

from aiohttp import web
import yaml


def _load_alert_dedup_module():
    """优先从当前项目路径加载本地 alert_dedup 模块。"""
    module_name = "aiops_alert_dedup"
    if module_name in sys.modules:
        return sys.modules[module_name]

    module_path = _project_root() / "toolsets" / "alert_dedup.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块: {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _project_root() -> Path:
    """返回项目根目录。"""
    return Path(__file__).resolve().parent.parent


alert_dedup = _load_alert_dedup_module()


def _load_incident_store_module():
    """优先从当前项目路径加载本地 incident_store 模块。"""
    module_name = "aiops_incident_store"
    if module_name in sys.modules:
        return sys.modules[module_name]

    module_path = _project_root() / "toolsets" / "incident_store.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块: {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


incident_store = _load_incident_store_module()


def _load_audit_log_module():
    """优先从当前项目路径加载本地 audit_log 模块。"""
    module_name = "aiops_audit_log"
    if module_name in sys.modules:
        return sys.modules[module_name]

    module_path = _project_root() / "toolsets" / "audit_log.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块: {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


audit_log = _load_audit_log_module()


def _load_k8s_read_module():
    """加载 k8s_read 模块，兼容其包内相对导入。"""
    module_name = "toolsets.k8s_read"
    if module_name in sys.modules:
        return sys.modules[module_name]

    project_root = _project_root()
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    return __import__(module_name, fromlist=["k8s_read"])


k8s_read_tool = _load_k8s_read_module()


def _load_prometheus_query_module():
    """加载 prometheus_query 模块。"""
    module_name = "toolsets.prometheus_query"
    if module_name in sys.modules:
        return sys.modules[module_name]

    project_root = _project_root()
    toolsets_dir = project_root / "toolsets"
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    if str(toolsets_dir) not in sys.path:
        sys.path.insert(0, str(toolsets_dir))
    return __import__(module_name, fromlist=["prometheus_query"])


prometheus_query_tool = _load_prometheus_query_module()


def _load_loki_query_module():
    """加载 loki_query 模块。"""
    module_name = "toolsets.loki_query"
    if module_name in sys.modules:
        return sys.modules[module_name]

    project_root = _project_root()
    toolsets_dir = project_root / "toolsets"
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    if str(toolsets_dir) not in sys.path:
        sys.path.insert(0, str(toolsets_dir))
    return __import__(module_name, fromlist=["loki_query"])


loki_query_tool = _load_loki_query_module()


def _load_feishu_conversation_module():
    """优先从当前项目路径加载本地 feishu_conversation 模块。"""
    module_name = "aiops_feishu_conversation"
    if module_name in sys.modules:
        return sys.modules[module_name]

    module_path = _project_root() / "hooks" / "feishu_conversation.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块: {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


feishu_conversation = _load_feishu_conversation_module()


def _config_path() -> Path:
    """返回配置文件路径。"""
    return _project_root() / "config.yaml"


def _load_config_sync() -> Dict[str, Any]:
    """同步读取配置。"""
    path = _config_path()
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return data if isinstance(data, dict) else {}


async def _load_config() -> Dict[str, Any]:
    """异步读取配置。"""
    return await asyncio.to_thread(_load_config_sync)


def _resolve_hmac_secret(config: Dict[str, Any]) -> str | None:
    """从环境变量或配置中解析 webhook HMAC 密钥。"""
    env_secret = os.getenv("ALERTMANAGER_WEBHOOK_SECRET")
    if env_secret:
        return env_secret

    candidates = [
        (("alertmanager_webhook", "secret"),),
        (("webhooks", "alertmanager", "secret"),),
        (("hooks", "alertmanager", "secret"),),
    ]
    for group in candidates:
        for path in group:
            current: Any = config
            for key in path:
                if not isinstance(current, dict):
                    current = None
                    break
                current = current.get(key)
            if isinstance(current, str) and current:
                return current
    return None


def _verify_hmac_signature(body: bytes, secret: str, signature: str | None) -> bool:
    """校验 webhook HMAC 签名。"""
    if not signature:
        return False
    received = signature.strip()
    if received.startswith("sha256="):
        received = received.split("=", 1)[1]
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(received, expected)


def _extract_alert(alert: Dict[str, Any]) -> Dict[str, Any]:
    """从 Alertmanager 单条告警中提取标准字段。"""
    labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
    annotations = alert.get("annotations") if isinstance(alert.get("annotations"), dict) else {}
    return {
        "alertname": str(labels.get("alertname", "")).strip(),
        "severity": str(labels.get("severity", "info")).strip().lower() or "info",
        "namespace": str(labels.get("namespace", "default")).strip() or "default",
        "cluster": str(labels.get("cluster", "default")).strip() or "default",
        "description": str(
            annotations.get("description") or annotations.get("summary") or ""
        ).strip(),
        "status": str(alert.get("status", "")).strip().lower(),
    }


def _build_dedup_key(alert: Dict[str, Any]) -> str:
    """构造 incident dedup key。"""
    return "|".join([alert["alertname"], alert["namespace"], alert["cluster"]])


def _dedup_key_version(config: Dict[str, Any]) -> str:
    """读取 dedup key 版本。"""
    sre = config.get("sre") if isinstance(config.get("sre"), dict) else {}
    return str(sre.get("dedup_key_version", "v1"))


def _build_triage_prompt(alert: Dict[str, Any], incident_id: str) -> str:
    """格式化 triage 提示词。"""
    return (
        f"[Incident {incident_id}] [Alertmanager] {alert['severity']} 告警: {alert['alertname']} "
        f"in {alert['namespace']}/{alert['cluster']}. {alert['description']}. 请执行 triage 流程。"
    )


async def _persist_case_profile_for_incident(incident: Dict[str, Any]) -> None:
    """为已收口 incident 沉淀 case profile。"""
    upsert_case_profile = getattr(incident_store, "upsert_case_profile", None)
    get_analysis = getattr(incident_store, "get_analysis", None)
    list_evidence = getattr(incident_store, "list_evidence", None)
    find_similar_case_profiles = getattr(incident_store, "find_similar_case_profiles", None)
    if upsert_case_profile is None or get_analysis is None or list_evidence is None:
        return

    incident_id = str(incident.get("id", ""))
    if not incident_id:
        return

    analysis = await get_analysis(incident_id) or {}
    evidence_rows = await list_evidence(incident_id)
    likely_scope = str(analysis.get("likely_scope") or "unknown")
    root_causes = analysis.get("suspected_root_causes") or []
    top_root_cause = str(root_causes[0].get("summary", "")) if root_causes else None
    effective_actions = [str(item) for item in (analysis.get("next_best_actions") or [])]
    metrics_evidence = next((item for item in evidence_rows if item.get("source_type") == "metrics_window"), None)
    audit_evidence = next((item for item in evidence_rows if item.get("source_type") == "audit_change"), None)
    metric_delta_summary = dict(metrics_evidence.get("payload") or {}) if metrics_evidence else {}
    change_clue_summary = str(audit_evidence.get("summary") or "") if audit_evidence else None
    created_at = float(incident.get("created_at") or 0.0)
    resolved_at = float(incident.get("resolved_at") or time.time())
    resolution_seconds = (resolved_at - created_at) if created_at > 0 else None
    incident_signature = f"{incident.get('alert_name', '')}|{incident.get('namespace', '')}|{likely_scope}|resolved"
    similar_incident_ids: list[str] = []
    if find_similar_case_profiles is not None:
        similar_profiles = await find_similar_case_profiles(
            incident_signature,
            exclude_incident_id=incident_id,
            limit=3,
        )
        similar_incident_ids = [str(item.get("incident_id", "")) for item in similar_profiles if item.get("incident_id")]

    await upsert_case_profile(
        incident_id,
        incident_signature=incident_signature,
        symptom_fingerprint="+".join(str(item) for item in (analysis.get("symptoms") or [])) or None,
        final_scope=likely_scope,
        final_root_cause=top_root_cause,
        effective_actions=effective_actions,
        invalid_actions=[],
        metric_delta_summary=metric_delta_summary,
        change_clue_summary=change_clue_summary,
        resolution_seconds=resolution_seconds,
        similar_incident_ids=similar_incident_ids,
    )


async def _seed_initial_observability_context(incident_id: str, alert: Dict[str, Any]) -> None:
    """为新进入调查流程的 incident 播种 Phase 2 结构化上下文。"""
    add_evidence = getattr(incident_store, "add_evidence", None)
    upsert_analysis = getattr(incident_store, "upsert_analysis", None)
    if add_evidence is None or upsert_analysis is None:
        return

    now = time.time()
    await add_evidence(
        incident_id,
        source_type="alert_window",
        source_ref=f"alertmanager/{alert['namespace']}/{alert['alertname']}",
        summary=f"{alert['severity']} alert entered firing state",
        payload=alert,
        window_start_ts=now - 300,
        window_end_ts=now + 300,
        collector_version="phase2.v1",
        confidence=0.9,
    )

    confidence = 0.2
    query_audit = getattr(audit_log, "query_audit", None)
    recent_change_count = 0
    recent_change_summary = "最近时间窗内未发现审计变更线索"
    has_recent_change = False
    if query_audit is not None:
        try:
            audit_rows = await query_audit(
                time_start=now - 1800,
                time_end=now,
                cluster=alert["cluster"],
                namespace=alert["namespace"],
                limit=20,
            )
        except Exception:
            audit_rows = []
        recent_change_count = len(audit_rows)
        if audit_rows:
            has_recent_change = True
            confidence += 0.15
            recent_change_summary = f"最近 {recent_change_count} 条变更线索，最新动作: {audit_rows[0].get('what', '')}"
            await add_evidence(
                incident_id,
                source_type="audit_change",
                source_ref=f"audit/{alert['cluster']}/{alert['namespace']}",
                summary=recent_change_summary,
                payload={"count": recent_change_count, "items": audit_rows[:5]},
                window_start_ts=now - 1800,
                window_end_ts=now,
                collector_version="phase2.v1",
                confidence=0.7,
            )
    missing_evidence = ["缺少 Kubernetes events", "缺少关键指标片段", "缺少最近变更线索"]
    supporting_evidence = [
        {
            "source_type": "alert_window",
            "summary": alert["description"] or f"{alert['alertname']} entered firing state",
        },
        {"source_type": "audit_change", "summary": recent_change_summary},
    ]
    suspected_root_causes = [{"summary": "等待更多证据收敛根因", "confidence": 0.2}]
    next_best_actions = ["采集 incident 时间窗内的指标、事件与变更线索"]
    has_resource_pressure = False
    has_log_clue = False

    k8s_read = getattr(k8s_read_tool, "k8s_read", None)
    recent_events_summary = ""
    has_k8s_event_clue = False
    if k8s_read is not None:
        try:
            event_result = await k8s_read(f"kubectl get events -n {alert['namespace']} --sort-by=.lastTimestamp")
        except Exception:
            event_result = {"ok": False, "stdout": "", "stderr": ""}
        if event_result.get("ok"):
            stdout = str(event_result.get("stdout") or "").strip()
            event_lines = [line.strip() for line in stdout.splitlines() if line.strip()]
            recent_event_lines = event_lines[-2:] if len(event_lines) > 1 else event_lines
            if recent_event_lines:
                has_k8s_event_clue = True
                confidence += 0.2
                recent_events_summary = " | ".join(recent_event_lines)
                await add_evidence(
                    incident_id,
                    source_type="k8s_events",
                    source_ref=f"k8s/events/{alert['namespace']}",
                    summary=recent_events_summary,
                    payload={"line_count": len(recent_event_lines), "lines": recent_event_lines},
                    window_start_ts=now - 600,
                    window_end_ts=now,
                    collector_version="phase2.v1",
                    confidence=0.8,
                )
                supporting_evidence.append({"source_type": "k8s_events", "summary": recent_events_summary})
                missing_evidence = [item for item in missing_evidence if item != "缺少 Kubernetes events"]

    has_topology_clue = False
    has_node_clue = False
    if k8s_read is not None:
        try:
            pods_result = await k8s_read(f"kubectl get pods -n {alert['namespace']}")
            deploy_result = await k8s_read(f"kubectl get deploy -n {alert['namespace']}")
            nodes_result = await k8s_read("kubectl get nodes")
        except Exception:
            pods_result = {"ok": False, "stdout": "", "stderr": ""}
            deploy_result = {"ok": False, "stdout": "", "stderr": ""}
            nodes_result = {"ok": False, "stdout": "", "stderr": ""}
        pod_lines = [line.strip() for line in str(pods_result.get("stdout") or "").splitlines() if line.strip()]
        deploy_lines = [line.strip() for line in str(deploy_result.get("stdout") or "").splitlines() if line.strip()]
        node_lines = [line.strip() for line in str(nodes_result.get("stdout") or "").splitlines() if line.strip()]
        if len(pod_lines) > 1 or len(deploy_lines) > 1:
            has_topology_clue = True
            confidence += 0.15
            topology_summary_parts = []
            if len(pod_lines) > 1:
                topology_summary_parts.append("pods: " + " | ".join(pod_lines[1:3]))
            if len(deploy_lines) > 1:
                topology_summary_parts.append("deployments: " + " | ".join(deploy_lines[1:3]))
            topology_summary = "; ".join(topology_summary_parts)
            await add_evidence(
                incident_id,
                source_type="workload_topology",
                source_ref=f"k8s/topology/{alert['namespace']}",
                summary=topology_summary,
                payload={
                    "pod_line_count": max(0, len(pod_lines) - 1),
                    "deploy_line_count": max(0, len(deploy_lines) - 1),
                    "pod_lines": pod_lines[1:3],
                    "deploy_lines": deploy_lines[1:3],
                },
                window_start_ts=now - 600,
                window_end_ts=now,
                collector_version="phase2.v1",
                confidence=0.75,
            )
            supporting_evidence.append({"source_type": "workload_topology", "summary": topology_summary})
        if len(node_lines) > 1:
            has_node_clue = True
            confidence += 0.1
            node_summary = "nodes: " + " | ".join(node_lines[1:3])
            await add_evidence(
                incident_id,
                source_type="node_status",
                source_ref="k8s/nodes",
                summary=node_summary,
                payload={
                    "node_line_count": max(0, len(node_lines) - 1),
                    "node_lines": node_lines[1:3],
                },
                window_start_ts=now - 600,
                window_end_ts=now,
                collector_version="phase2.v1",
                confidence=0.7,
            )
            supporting_evidence.append({"source_type": "node_status", "summary": node_summary})

    if has_recent_change:
        suspected_root_causes.append({"summary": "近期变更可能引发工作负载异常", "confidence": 0.55})
        next_best_actions.append("核对最近 30 分钟内的发布或变更")

    if has_k8s_event_clue:
        suspected_root_causes.append({"summary": "Kubernetes events 显示工作负载异常", "confidence": 0.65})
        next_best_actions.append("检查异常 Pod 的事件与探针失败细节")

    if has_topology_clue:
        suspected_root_causes.append({"summary": "工作负载拓扑状态显示副本或 Pod 异常", "confidence": 0.7})
        next_best_actions.append("检查 Deployment 可用副本与异常 Pod 状态")

    if has_node_clue:
        suspected_root_causes.append({"summary": "节点状态异常可能扩大影响范围", "confidence": 0.6})
        next_best_actions.append("检查异常 Node 状态与受影响工作负载分布")

    prometheus_query = getattr(prometheus_query_tool, "prometheus_query", None)
    if prometheus_query is not None:
        metrics_start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 900))
        metrics_end = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        cpu_query = (
            f'rate(container_cpu_usage_seconds_total{{namespace="{alert["namespace"]}"}}[5m])'
        )
        memory_query = (
            f'container_memory_working_set_bytes{{namespace="{alert["namespace"]}"}}'
        )
        restart_query = (
            f'kube_pod_container_status_restarts_total{{namespace="{alert["namespace"]}"}}'
        )
        readiness_query = (
            f'kube_pod_status_ready{{namespace="{alert["namespace"]}",condition="false"}}'
        )
        try:
            cpu_result = await prometheus_query(cpu_query, start=metrics_start, end=metrics_end)
            memory_result = await prometheus_query(memory_query, start=metrics_start, end=metrics_end)
            restart_result = await prometheus_query(restart_query, start=metrics_start, end=metrics_end)
            readiness_result = await prometheus_query(readiness_query, start=metrics_start, end=metrics_end)
        except Exception:
            cpu_result = {"allowed": False, "results": []}
            memory_result = {"allowed": False, "results": []}
            restart_result = {"allowed": False, "results": []}
            readiness_result = {"allowed": False, "results": []}

        cpu_values = cpu_result.get("results") or []
        memory_values = memory_result.get("results") or []
        restart_values = restart_result.get("results") or []
        readiness_values = readiness_result.get("results") or []
        cpu_max = None
        memory_max = None
        restart_max = None
        unready_count = None
        if cpu_values:
            cpu_series = cpu_values[0].get("values") or []
            if cpu_series:
                cpu_max = str(cpu_series[-1][1])
        if memory_values:
            memory_series = memory_values[0].get("values") or []
            if memory_series:
                memory_max = str(memory_series[-1][1])
        if restart_values:
            restart_series = restart_values[0].get("values") or []
            if restart_series:
                restart_max = str(restart_series[-1][1])
        if readiness_values:
            readiness_series = readiness_values[0].get("values") or []
            if readiness_series:
                unready_count = str(readiness_series[-1][1])

        if cpu_max is not None or memory_max is not None or restart_max is not None or unready_count is not None:
            metrics_summary = (
                f"cpu_max={cpu_max or 'n/a'}, memory_max={memory_max or 'n/a'}, "
                f"restart_max={restart_max or 'n/a'}, unready_count={unready_count or 'n/a'}"
            )
            await add_evidence(
                incident_id,
                source_type="metrics_window",
                source_ref=f"prometheus/{alert['namespace']}",
                summary=metrics_summary,
                payload={
                    "cpu_query": cpu_query,
                    "memory_query": memory_query,
                    "restart_query": restart_query,
                    "readiness_query": readiness_query,
                    "cpu_max": cpu_max,
                    "memory_max": memory_max,
                    "restart_max": restart_max,
                    "unready_count": unready_count,
                },
                window_start_ts=now - 900,
                window_end_ts=now,
                collector_version="phase2.v1",
                confidence=0.75,
            )
            supporting_evidence.append({"source_type": "metrics_window", "summary": metrics_summary})
            try:
                cpu_high = cpu_max is not None and float(cpu_max) >= 0.8
            except (TypeError, ValueError):
                cpu_high = False
            try:
                memory_high = memory_max is not None and float(memory_max) >= 8.0e8
            except (TypeError, ValueError):
                memory_high = False
            try:
                restart_high = restart_max is not None and float(restart_max) >= 5.0
            except (TypeError, ValueError):
                restart_high = False
            try:
                unready_high = unready_count is not None and float(unready_count) >= 1.0
            except (TypeError, ValueError):
                unready_high = False
            if cpu_high or memory_high:
                has_resource_pressure = True
                confidence += 0.2
            if restart_high or unready_high:
                confidence += 0.25
                suspected_root_causes.append({"summary": "工作负载健康状态异常", "confidence": 0.75})
                next_best_actions.append("检查 Pod 重启次数与 Ready 状态")

    if has_resource_pressure:
        suspected_root_causes.append({"summary": "资源压力可能导致工作负载异常", "confidence": 0.7})
        next_best_actions.append("检查 Pod CPU/内存指标与资源配置")

    loki_query = getattr(loki_query_tool, "loki_query", None)
    if loki_query is not None:
        logs_start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 900))
        logs_end = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        logs_query = f'{{namespace="{alert["namespace"]}"}}'
        try:
            logs_result = await loki_query(logs_query, start=logs_start, end=logs_end, limit=20)
        except Exception:
            logs_result = {"allowed": False, "results": []}

        log_streams = logs_result.get("results") or []
        if log_streams:
            collected_lines: list[str] = []
            for stream in log_streams[:2]:
                for value in stream.get("values") or []:
                    if len(value) >= 2:
                        collected_lines.append(str(value[1]).strip())
                    if len(collected_lines) >= 2:
                        break
                if len(collected_lines) >= 2:
                    break
            if collected_lines:
                has_log_clue = True
                confidence += 0.15
                logs_summary = " | ".join(collected_lines)
                await add_evidence(
                    incident_id,
                    source_type="logs_window",
                    source_ref=f"loki/{alert['namespace']}",
                    summary=logs_summary,
                    payload={"query": logs_query, "line_count": len(collected_lines), "lines": collected_lines},
                    window_start_ts=now - 900,
                    window_end_ts=now,
                    collector_version="phase2.v1",
                    confidence=0.7,
                )
                supporting_evidence.append({"source_type": "logs_window", "summary": logs_summary})
                suspected_root_causes.append({"summary": "应用日志显示运行时异常", "confidence": 0.7})
                next_best_actions.append("检查相关 Pod 最近错误日志与超时信息")

    if has_log_clue:
        missing_evidence = [item for item in missing_evidence if item != "缺少 pod 日志摘要"]

    confidence = min(confidence, 0.95)

    await upsert_analysis(
        incident_id,
        symptoms=[f"{alert['alertname']} firing in {alert['namespace']}/{alert['cluster']}"],
        likely_scope="workload",
        suspected_root_causes=suspected_root_causes,
        supporting_evidence=supporting_evidence,
        missing_evidence=missing_evidence,
        next_best_actions=next_best_actions,
        confidence=confidence,
    )


async def _handle_resolved_alert(
    alert: Dict[str, Any],
    config: Dict[str, Any],
) -> Dict[str, Any] | None:
    """处理 Alertmanager resolved 告警并更新已有 incident。"""
    dedup_key = _build_dedup_key(alert)
    dedup_key_version = _dedup_key_version(config)
    existing = await incident_store.find_reusable_incident(dedup_key, dedup_key_version)
    if existing is None:
        return None

    incident_id = str(existing["id"])
    await incident_store.add_event(
        incident_id,
        "resolved",
        "alert_webhook",
        alert["alertname"],
        alert["description"] or "Alertmanager resolved",
        alert,
    )
    current_status = str(existing.get("status", "")).strip().lower()
    if current_status != "resolved":
        resolved_at = time.time()
        await incident_store.update_status(incident_id, "resolved", resolved_at=resolved_at)
        existing = {**existing, "resolved_at": resolved_at, "status": "resolved"}

    await _persist_case_profile_for_incident(existing)

    return {"incident_id": incident_id, "event_type": "resolved", "dedup_key": dedup_key}


async def _handle_alertmanager(request: web.Request) -> web.Response:
    """处理 Alertmanager webhook 请求。"""
    config = request.app.get("alert_webhook_config")
    if config is None:
        config = await _load_config()
        request.app["alert_webhook_config"] = config

    body = await request.read()
    secret = _resolve_hmac_secret(config)
    if secret:
        signature = request.headers.get("X-Signature") or request.headers.get("X-Hub-Signature-256")
        if not _verify_hmac_signature(body, secret, signature):
            return web.json_response({"ok": False, "message": "签名校验失败"}, status=401)

    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except json.JSONDecodeError:
        return web.json_response({"ok": False, "message": "无效的 JSON payload"}, status=400)

    result = await handle_alertmanager_payload(payload, dict(request.headers), config)
    return web.json_response(result)


async def handle_alertmanager_payload(
    payload: Dict[str, Any],
    headers: Dict[str, str] | None = None,
    config: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """处理已解析的 Alertmanager payload，供 Hermes gateway 直接复用。"""
    del headers
    if config is None:
        config = await _load_config()

    raw_alerts = payload.get("alerts") if isinstance(payload.get("alerts"), list) else []
    prompts: List[str] = []
    incidents: List[Dict[str, Any]] = []
    processed = 0
    skipped = 0

    for raw_alert in raw_alerts:
        if not isinstance(raw_alert, dict):
            skipped += 1
            continue

        alert = _extract_alert(raw_alert)
        if alert["status"] == "resolved":
            resolved_incident = await _handle_resolved_alert(alert, config)
            if resolved_incident is None:
                skipped += 1
            else:
                processed += 1
                incidents.append(resolved_incident)
            continue

        if await alert_dedup.should_process(alert):
            dedup_key = _build_dedup_key(alert)
            dedup_key_version = _dedup_key_version(config)
            existing = await incident_store.find_reusable_incident(dedup_key, dedup_key_version)
            if existing is None:
                incident_id = await incident_store.create_incident(
                    alert["alertname"],
                    alert["namespace"],
                    alert["cluster"],
                    alert["description"],
                    platform="feishu",
                    dedup_key=dedup_key,
                    dedup_key_version=dedup_key_version,
                )
            else:
                incident_id = str(existing["id"])

            await incident_store.add_event(
                incident_id,
                "alert_fired",
                "alert_webhook",
                alert["alertname"],
                alert["description"],
                alert,
            )
            await _seed_initial_observability_context(incident_id, alert)
            feishu_binding = await feishu_conversation.publish_incident_status(incident_id, alert, config)
            if feishu_binding.get("chat_id"):
                await incident_store.update_feishu_binding(incident_id, **feishu_binding)
            processed += 1
            prompts.append(_build_triage_prompt(alert, incident_id))
            incidents.append(
                {
                    "incident_id": incident_id,
                    "event_type": "alert_fired",
                    "dedup_key": dedup_key,
                    "feishu_binding": feishu_binding,
                }
            )
        else:
            skipped += 1

    return {
        "ok": True,
        "processed": processed,
        "skipped": skipped,
        "prompts": prompts,
        "incidents": incidents,
    }


async def setup_alert_webhook(app: web.Application) -> None:
    """在 aiohttp 应用中注册 Alertmanager webhook 路由。"""
    if "alert_webhook_config" not in app:
        app["alert_webhook_config"] = await _load_config()
    app.router.add_post("/webhooks/alertmanager", _handle_alertmanager)
