"""Alertmanager Webhook 处理 Hook。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import shlex
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


def _load_approval_async_module():
    """优先从当前项目路径加载本地 approval_async 模块。"""
    module_name = "aiops_approval_async"
    if module_name in sys.modules:
        return sys.modules[module_name]

    module_path = _project_root() / "toolsets" / "approval_async.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块: {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


approval_async = _load_approval_async_module()


def _load_feishu_native_approval_module():
    """优先从当前项目路径加载本地 feishu_native_approval 模块。"""
    module_name = "aiops_feishu_native_approval"
    if module_name in sys.modules:
        return sys.modules[module_name]

    module_path = _project_root() / "toolsets" / "feishu_native_approval.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块: {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


feishu_native_approval = _load_feishu_native_approval_module()


def _load_remediation_plan_module():
    """优先从当前项目路径加载本地 remediation_plan 模块。"""
    module_name = "aiops_remediation_plan"
    if module_name in sys.modules:
        return sys.modules[module_name]

    module_path = _project_root() / "toolsets" / "remediation_plan.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块: {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


remediation_plan = _load_remediation_plan_module()


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


def _load_message_delivery_module():
    """优先从当前项目路径加载本地 message_delivery 模块。"""
    module_name = "aiops_message_delivery"
    if module_name in sys.modules:
        return sys.modules[module_name]

    module_path = _project_root() / "toolsets" / "message_delivery.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块: {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


message_delivery = _load_message_delivery_module()


def _load_incident_analysis_summary_module():
    """优先从当前项目路径加载本地 incident_analysis_summary 模块。"""
    module_name = "aiops_incident_analysis_summary"
    if module_name in sys.modules:
        return sys.modules[module_name]

    module_path = _project_root() / "hooks" / "incident_analysis_summary.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块: {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


incident_analysis_summary = _load_incident_analysis_summary_module()


def _runtime_config_candidates() -> list[Path]:
    """返回运行时配置候选路径，按优先级排序。"""
    candidates: list[Path] = []

    hermes_config = os.getenv("HERMES_CONFIG")
    if hermes_config:
        candidates.append(Path(hermes_config).expanduser())

    hermes_home = os.getenv("HERMES_HOME")
    if hermes_home:
        candidates.append(Path(hermes_home).expanduser() / "config.yaml")

    return candidates


def _load_config_sync() -> Dict[str, Any]:
    """同步读取运行时配置。"""
    for path in _runtime_config_candidates():
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        return data if isinstance(data, dict) else {}
    return {}


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


def _pick_first_text(*values: Any) -> str | None:
    """返回首个非空文本值。"""
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_target_fields(labels: Dict[str, Any], annotations: Dict[str, Any]) -> Dict[str, str | None]:
    """提取 Pod、容器和工作负载目标字段。"""
    pod_name = _pick_first_text(labels.get("pod"), labels.get("pod_name"), annotations.get("pod"))
    container_name = _pick_first_text(
        labels.get("container"), labels.get("container_name"), annotations.get("container")
    )
    workload_pairs = (
        ("Deployment", _pick_first_text(labels.get("deployment"), labels.get("deployment_name"))),
        ("StatefulSet", _pick_first_text(labels.get("statefulset"), labels.get("statefulset_name"))),
        ("DaemonSet", _pick_first_text(labels.get("daemonset"), labels.get("daemonset_name"))),
        ("CronJob", _pick_first_text(labels.get("cronjob"), labels.get("cronjob_name"))),
        ("Job", _pick_first_text(labels.get("job_name"))),
    )
    for workload_kind, workload_name in workload_pairs:
        if workload_name:
            return {
                "pod_name": pod_name,
                "container_name": container_name,
                "workload_kind": workload_kind,
                "workload_name": workload_name,
            }
    return {
        "pod_name": pod_name,
        "container_name": container_name,
        "workload_kind": None,
        "workload_name": _pick_first_text(
            annotations.get("workload_name"),
            labels.get("app.kubernetes.io/name"),
            labels.get("app"),
        ),
    }


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
        **_extract_target_fields(labels, annotations),
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


def _initial_analysis() -> Dict[str, List[Any]]:
    """初始化最小化 incident 分析结构。"""
    return {
        "suspected_root_causes": [],
        "supporting_evidence": [],
        "missing_evidence": ["缺少 pod 日志摘要"],
        "next_best_actions": [],
    }


def _pick_approval_action(analysis: Dict[str, Any], alert: Dict[str, Any] | None = None) -> str | None:
    """从分析结果里选择一个最小可审批动作。"""
    actions = analysis.get("next_best_actions")
    if not isinstance(actions, list):
        return None
    first_action: str | None = None
    for action in actions:
        if not isinstance(action, str):
            continue
        stripped = action.strip()
        if not stripped:
            continue
        if first_action is None:
            first_action = stripped
        context = remediation_plan.build_remediation_context(
            stripped,
            incident_id="action-selection",
            alertname=(alert or {}).get("alertname"),
            cluster=(alert or {}).get("cluster"),
            namespace=(alert or {}).get("namespace"),
        )
        if context.get("executable") is True:
            return stripped
    return first_action


def _approval_operation_type(action: str) -> str:
    lowered = action.lower()
    if "exec" in lowered or "进入" in action:
        return "k8s_exec"
    return "k8s_write"


def _approval_risk_level(operation_type: str, action: str) -> str:
    lowered = action.lower()
    if operation_type == "k8s_exec":
        return "elevated"
    if any(word in lowered for word in ("delete", "namespace", "node", "pv")):
        return "dangerous"
    if any(word in action for word in ("删除", "命名空间", "节点")):
        return "dangerous"
    return "standard"


def _remediation_max_replicas(config: Dict[str, Any] | None) -> int:
    """读取 remediation schema 的最大副本数限制。"""
    if not isinstance(config, dict):
        return remediation_plan.DEFAULT_MAX_REPLICAS

    candidates = []
    remediation = config.get("remediation")
    if isinstance(remediation, dict):
        candidates.append(remediation.get("max_replicas"))
    sre = config.get("sre")
    if isinstance(sre, dict) and isinstance(sre.get("remediation"), dict):
        candidates.append(sre["remediation"].get("max_replicas"))

    for value in candidates:
        try:
            max_replicas = int(value)
        except (TypeError, ValueError):
            continue
        if max_replicas >= 0:
            return max_replicas
    return remediation_plan.DEFAULT_MAX_REPLICAS


def _first_matching_lines(output: str, keywords: List[str], limit: int = 3) -> List[str]:
    """返回首批包含关键字的行，便于生成紧凑摘要。"""
    if not output.strip():
        return []

    matched: List[str] = []
    lowered_keywords = [keyword.lower() for keyword in keywords if keyword]
    for line in output.splitlines():
        text = line.strip()
        if not text:
            continue
        lowered = text.lower()
        if any(keyword in lowered for keyword in lowered_keywords):
            matched.append(text)
            if len(matched) >= limit:
                return matched
    return matched


def _summarize_targeted_output(command: str, output: str) -> str:
    """对 targeted kubectl 输出做最小摘要。"""
    highlights = _first_matching_lines(
        output,
        [
            "error",
            "fail",
            "back-off",
            "crashloop",
            "oomkilled",
            "warning",
            "unhealthy",
            "restart",
            "terminat",
        ],
    )
    if highlights:
        return " | ".join(highlights)

    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return f"{command} 无输出"
    return " | ".join(lines[:3])


def _targeted_log_command(alert: Dict[str, Any]) -> str | None:
    """构造针对 pod/container 的日志命令。"""
    pod_name = alert.get("pod_name")
    namespace = alert.get("namespace")
    if not pod_name or not namespace:
        return None

    command = f"kubectl logs {pod_name} -n {namespace}"
    if alert.get("container_name"):
        command += f" --container {alert['container_name']}"
    command += " --tail=50 --since=15m"
    return command


async def _run_kubectl_command(command: str) -> Dict[str, Any]:
    """执行只读 kubectl 命令并返回基础结果。"""
    tokens = shlex.split(command)
    if not tokens or tokens[0] != "kubectl":
        raise ValueError("仅允许执行 kubectl 命令")

    try:
        process = await asyncio.create_subprocess_exec(
            *tokens,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return {
            "ok": False,
            "stdout": "",
            "stderr": f"kubectl 启动失败: {exc}",
        }

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        return {"ok": False, "stdout": "", "stderr": "kubectl 执行超时（60s）"}

    return {
        "ok": process.returncode == 0,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
    }


def _append_unique(items: List[str], value: str) -> None:
    """避免重复写入简单字符串分析项。"""
    if value not in items:
        items.append(value)


def _guess_likely_scope(alert: Dict[str, Any], analysis: Dict[str, List[Any]]) -> str:
    """基于现有目标字段推断 incident scope。"""
    if alert.get("pod_name") or alert.get("workload_name"):
        return "workload"
    for item in analysis.get("supporting_evidence", []):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or item.get("source_type") or "").lower()
        if kind.startswith("pod_") or kind == "workload":
            return "workload"
    return "namespace"


def _normalize_root_causes(items: List[Any]) -> List[Dict[str, Any]]:
    """统一 root cause 条目格式，兼容字符串与结构化对象。"""
    normalized: List[Dict[str, Any]] = []
    for item in items:
        if isinstance(item, str) and item.strip():
            normalized.append({"summary": item.strip(), "confidence": None})
        elif isinstance(item, dict):
            summary = item.get("summary")
            if isinstance(summary, str) and summary.strip():
                normalized.append(
                    {
                        "summary": summary.strip(),
                        "confidence": item.get("confidence"),
                    }
                )
    return normalized


async def _persist_incident_analysis_context(
    incident_id: str,
    alert: Dict[str, Any],
    analysis: Dict[str, List[Any]],
) -> None:
    """将当前 webhook 已有 analysis 结果落到正式存储。"""
    add_evidence = getattr(incident_store, "add_evidence", None)
    upsert_analysis = getattr(incident_store, "upsert_analysis", None)
    if add_evidence is None or upsert_analysis is None:
        return

    now = time.time()
    await add_evidence(
        incident_id,
        source_type="alert_window",
        source_ref=f"alertmanager/{alert['namespace']}/{alert['alertname']}",
        summary=alert["description"] or f"{alert['alertname']} entered firing state",
        payload=alert,
        window_start_ts=now - 300,
        window_end_ts=now + 300,
        collector_version="phase2.v1",
        confidence=0.9,
    )

    for item in analysis.get("supporting_evidence", []):
        if not isinstance(item, dict):
            continue
        source_type = str(item.get("kind") or "analysis_evidence")
        summary = str(item.get("summary") or "").strip()
        if not summary:
            continue
        await add_evidence(
            incident_id,
            source_type=source_type,
            source_ref=item.get("source"),
            summary=summary,
            payload=item,
            collected_at=now,
            collector_version="phase2.v1",
            confidence=None,
        )

    await upsert_analysis(
        incident_id,
        symptoms=[f"{alert['alertname']} firing in {alert['namespace']}/{alert['cluster']}"],
        likely_scope=_guess_likely_scope(alert, analysis),
        suspected_root_causes=_normalize_root_causes(analysis.get("suspected_root_causes", [])),
        supporting_evidence=[item for item in analysis.get("supporting_evidence", []) if isinstance(item, dict)],
        missing_evidence=[item for item in analysis.get("missing_evidence", []) if isinstance(item, str)],
        next_best_actions=[item for item in analysis.get("next_best_actions", []) if isinstance(item, str)],
        confidence=None,
    )


async def _attach_similar_case_recall(
    incident_id: str,
    alert: Dict[str, Any],
    analysis: Dict[str, List[Any]],
) -> None:
    """仅使用本地 SQLite 历史 profile 补充相似 case recall。"""
    find_similar_case_profiles = getattr(incident_store, "find_similar_case_profiles", None)
    list_recent_case_profiles = getattr(incident_store, "list_recent_case_profiles", None)
    add_evidence = getattr(incident_store, "add_evidence", None)
    upsert_analysis = getattr(incident_store, "upsert_analysis", None)
    if None in (find_similar_case_profiles, list_recent_case_profiles, add_evidence, upsert_analysis):
        return

    likely_scope = _guess_likely_scope(alert, analysis)
    signature = f"{alert['alertname']}|{alert['namespace']}|{likely_scope}|resolved"
    similar = await find_similar_case_profiles(signature, limit=3)
    if not similar:
        similar = await list_recent_case_profiles(namespace=alert["namespace"], final_scope=likely_scope, limit=3)
    if not similar:
        return

    top = similar[0]
    recall_summary = (
        f"历史相似 case: {top.get('incident_id')} 根因={top.get('final_root_cause') or 'unknown'}; "
        f"有效动作={', '.join(top.get('effective_actions') or []) or '无'}"
    )
    analysis.setdefault("supporting_evidence", []).append({"kind": "case_recall", "summary": recall_summary})
    if top.get("effective_actions"):
        suggested = f"参考历史相似 case: {top['effective_actions'][0]}"
        if suggested not in analysis.setdefault("next_best_actions", []):
            analysis["next_best_actions"].append(suggested)

    await add_evidence(
        incident_id,
        source_type="case_recall",
        source_ref=str(top.get("incident_id") or ""),
        summary=recall_summary,
        payload=top,
        collector_version="phase2.v1",
        confidence=0.5,
    )
    await upsert_analysis(
        incident_id,
        symptoms=[f"{alert['alertname']} firing in {alert['namespace']}/{alert['cluster']}"],
        likely_scope=likely_scope,
        suspected_root_causes=_normalize_root_causes(analysis.get("suspected_root_causes", [])),
        supporting_evidence=[item for item in analysis.get("supporting_evidence", []) if isinstance(item, dict)],
        missing_evidence=[item for item in analysis.get("missing_evidence", []) if isinstance(item, str)],
        next_best_actions=[item for item in analysis.get("next_best_actions", []) if isinstance(item, str)],
        confidence=None,
    )


def _native_approval_enabled(config: Dict[str, Any] | None) -> bool:
    platforms = config.get("platforms") if isinstance(config, dict) and isinstance(config.get("platforms"), dict) else {}
    feishu = platforms.get("feishu") if isinstance(platforms.get("feishu"), dict) else {}
    approval = feishu.get("approval") if isinstance(feishu.get("approval"), dict) else {}
    return bool(approval.get("enabled"))


def _approval_config(config: Dict[str, Any] | None) -> Dict[str, Any]:
    platforms = config.get("platforms") if isinstance(config, dict) and isinstance(config.get("platforms"), dict) else {}
    feishu = platforms.get("feishu") if isinstance(platforms.get("feishu"), dict) else {}
    approval = feishu.get("approval") if isinstance(feishu.get("approval"), dict) else {}
    return approval


def _incident_thread_binding(incident_id: str, alert: Dict[str, Any]) -> Dict[str, Any]:
    binding = alert.get("feishu_binding") if isinstance(alert.get("feishu_binding"), dict) else {}
    root_message_id = binding.get("root_message_id") or binding.get("status_card_message_id")
    return {
        "incident_id": incident_id,
        "chat_id": binding.get("chat_id"),
        "root_message_id": root_message_id,
        "thread_id": binding.get("thread_id") or root_message_id,
    }


def _stable_delivery_payload_hash(payload: Dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _native_approval_notice_delivery_payload(approval: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "target_type": "approval_notice",
        "msg_type": "text",
        "approval_id": approval.get("approval_id"),
        "external_instance_code": approval.get("external_instance_code"),
        "external_url": approval.get("external_url"),
        "operation_summary": approval.get("operation_summary") or approval.get("command"),
        "risk_level": approval.get("risk_level"),
    }


async def _publish_native_approval_notice(
    incident_id: str,
    alert: Dict[str, Any],
    approval_state: Dict[str, Any],
    *,
    action: str,
    risk_level: str,
    config: Dict[str, Any] | None,
) -> Dict[str, Any] | None:
    approval = dict(approval_state)
    approval.setdefault("command", action)
    approval.setdefault("risk_level", risk_level)
    approval.setdefault("operation_summary", action)
    approval_id = str(approval.get("approval_id") or "").strip()
    existing_message_id = str(approval.get("approval_message_id") or "").strip()
    incident = _incident_thread_binding(incident_id, alert)
    chat_id = str(incident.get("chat_id") or "").strip()
    thread_id = str(incident.get("thread_id") or incident.get("root_message_id") or "").strip()

    if not approval_id:
        return {
            "ok": False,
            "approval_message_id": None,
            "delivery_status": "failed",
            "message": "approval_id 不能为空",
        }

    payload_hash = _stable_delivery_payload_hash(_native_approval_notice_delivery_payload(approval))

    if existing_message_id:
        delivery_id = await message_delivery.upsert_delivery(
            incident_id=incident_id,
            target_type="approval_notice",
            platform="feishu",
            chat_id=chat_id,
            thread_id=thread_id or None,
            approval_id=approval_id,
            payload_hash=payload_hash,
        )
        await message_delivery.mark_sent(delivery_id, existing_message_id)
        return {
            "ok": True,
            "approval_id": approval_id,
            "approval_message_id": existing_message_id,
            "delivery_status": "sent",
            "delivery_id": delivery_id,
            "message_id": existing_message_id,
            "thread_id": thread_id or None,
        }

    sent_delivery = await message_delivery.find_sent_delivery_for_approval(
        approval_id=approval_id,
        target_type="approval_notice",
    )
    if sent_delivery is not None:
        target_message_id = str(sent_delivery.get("target_message_id") or "").strip()
        if target_message_id:
            update_message_id = getattr(approval_async, "update_approval_message_id", None)
            if callable(update_message_id):
                await update_message_id(approval_id, target_message_id)
            return {
                "ok": True,
                "approval_id": approval_id,
                "approval_message_id": target_message_id,
                "delivery_status": "sent",
                "delivery_id": sent_delivery.get("id"),
                "message_id": target_message_id,
                "thread_id": sent_delivery.get("thread_id") or thread_id or None,
            }

    if not chat_id or not thread_id:
        delivery_id = await message_delivery.upsert_delivery(
            incident_id=incident_id,
            target_type="approval_notice",
            platform="feishu",
            chat_id=chat_id,
            thread_id=thread_id or None,
            approval_id=approval_id,
            payload_hash=payload_hash,
        )
        await message_delivery.mark_failed(delivery_id, "incident 飞书 thread 绑定未就绪")
        return {
            "ok": True,
            "approval_id": approval_id,
            "approval_message_id": None,
            "delivery_status": "pending_retry",
            "delivery_id": delivery_id,
            "message": "incident 飞书 thread 绑定未就绪",
            "thread_id": thread_id or None,
        }

    delivery_id = await message_delivery.upsert_delivery(
        incident_id=incident_id,
        target_type="approval_notice",
        platform="feishu",
        chat_id=chat_id,
        thread_id=thread_id or None,
        approval_id=approval_id,
        payload_hash=payload_hash,
    )

    publisher = getattr(feishu_conversation, "publish_native_approval_notice", None)
    if not callable(publisher):
        await message_delivery.mark_failed(delivery_id, "feishu native approval notice publisher unavailable")
        return {
            "ok": True,
            "approval_id": approval_id,
            "approval_message_id": None,
            "delivery_status": "pending_retry",
            "delivery_id": delivery_id,
            "message": "feishu native approval notice publisher unavailable",
        }

    try:
        response = await publisher(incident, approval, config or {})
    except Exception as exc:
        await message_delivery.mark_failed(delivery_id, str(exc))
        return {
            "ok": True,
            "approval_id": approval_id,
            "approval_message_id": None,
            "delivery_status": "pending_retry",
            "delivery_id": delivery_id,
            "message": str(exc),
        }

    message_id = str(response.get("message_id") or "").strip()
    if not message_id:
        await message_delivery.mark_failed(delivery_id, "飞书原生审批通知未返回 message_id")
        return {
            "ok": True,
            "approval_id": approval_id,
            "approval_message_id": None,
            "delivery_status": "pending_retry",
            "delivery_id": delivery_id,
            "message": "飞书原生审批通知未返回 message_id",
            "thread_id": response.get("thread_id"),
        }

    await message_delivery.mark_sent(delivery_id, message_id)
    update_message_id = getattr(approval_async, "update_approval_message_id", None)
    if callable(update_message_id):
        await update_message_id(approval_id, message_id)
    result = dict(response)
    result.update(
        {
            "ok": True,
            "approval_id": approval_id,
            "approval_message_id": message_id,
            "delivery_status": "sent",
            "delivery_id": delivery_id,
            "message_id": message_id,
        }
    )
    return result


async def _maybe_request_phase3_approval(
    incident_id: str,
    alert: Dict[str, Any],
    analysis: Dict[str, Any],
    config: Dict[str, Any] | None = None,
) -> Dict[str, Any] | None:
    """把分析建议转成一个非阻塞审批请求。"""

    def _merge_delivery_result(
        approval_state: Dict[str, Any],
        delivery_result: Dict[str, Any] | None,
    ) -> Dict[str, Any]:
        if not delivery_result:
            return approval_state

        merged = dict(approval_state)
        for key, value in delivery_result.items():
            if value is not None:
                merged[key] = value
        return merged

    action = _pick_approval_action(analysis, alert)
    if not action:
        await incident_store.add_event(incident_id, "approval_skipped", "alert_webhook", "", "no_action")
        return None

    namespace = str(alert.get("namespace") or "default")
    context = remediation_plan.build_remediation_context(
        action,
        incident_id=incident_id,
        alertname=alert.get("alertname"),
        cluster=alert.get("cluster"),
        namespace=namespace,
        max_replicas=_remediation_max_replicas(config),
    )
    context.update(
        {
            "alertname": alert.get("alertname"),
            "namespace": namespace,
            "cluster": alert.get("cluster"),
            "source": "alert_webhook",
        }
    )
    remediation_action = context.get("remediation_action") if isinstance(context.get("remediation_action"), dict) else {}
    risk = remediation_action.get("risk") if isinstance(remediation_action.get("risk"), dict) else {}
    operation_type = str(risk.get("operation_type") or "manual_remediation")
    risk_level = str(risk.get("risk_level") or _approval_risk_level(operation_type, action))
    action_signature = str(context["action_signature"])
    existing = await approval_async.find_pending_approval(incident_id, action_signature)
    if existing:
        delivery_result = None
        approval_state = await approval_async.check_approval(existing["approval_id"])
        existing_status = str(existing.get("status") or approval_state.get("status") or "").strip().lower()
        external_provider = str(
            existing.get("external_provider") or approval_state.get("external_provider") or ""
        ).strip()
        is_external_pending = existing_status == "external_pending" or bool(external_provider)
        if is_external_pending and _native_approval_enabled(config):
            delivery_result = await _publish_native_approval_notice(
                incident_id,
                alert,
                approval_state,
                action=action,
                risk_level=risk_level,
                config=config,
            )
        elif not existing.get("approval_message_id"):
            publish_card = getattr(approval_async, "publish_or_queue_approval_card", None)
            if callable(publish_card):
                delivery_result = await publish_card(existing["approval_id"], config=config)
        return _merge_delivery_result(approval_state, delivery_result)

    if _native_approval_enabled(config):
        request_external = getattr(approval_async, "request_external_approval", None)
        if callable(request_external):
            approval_result = await request_external(
                operation_type,
                action,
                context,
                namespace,
                "alert_webhook",
                risk_level,
                incident_id=incident_id,
                config=config,
            )
            approval_id = str(approval_result.get("approval_id") or "").strip()
        else:
            approval_id = await approval_async.request_approval(
                operation_type,
                action,
                context,
                namespace,
                "alert_webhook",
                risk_level,
                incident_id=incident_id,
            )
            approval_result = {"ok": True, "approval_id": approval_id, "status": "pending"}

        create_instance = getattr(feishu_native_approval, "create_approval_instance", None)
        if callable(create_instance):
            native_result = await create_instance(
                approval_id=approval_id,
                operation_type=operation_type,
                command=action,
                context=context,
                namespace=namespace,
                requester_open_id=_approval_config(config).get("requester_open_id"),
                risk_level=risk_level,
                config=config or {},
            )
        else:
            native_result = {
                "ok": False,
                "error_type": "native_approval_unavailable",
                "message": "feishu native approval module unavailable",
            }

        if native_result.get("ok"):
            record_created = getattr(approval_async, "record_external_approval_created", None)
            if callable(record_created):
                approval_state = await record_created(
                    approval_id,
                    provider="feishu",
                    external_uuid=native_result.get("external_uuid") or approval_id,
                    external_approval_code=native_result.get("external_approval_code")
                    or _approval_config(config).get("approval_code"),
                    external_instance_code=native_result.get("external_instance_code"),
                    external_status=native_result.get("external_status") or "PENDING",
                    external_url=native_result.get("external_url"),
                )
            else:
                approval_state = dict(native_result)
                approval_state.update({"approval_id": approval_id, "status": "external_pending"})
            checked = await approval_async.check_approval(approval_id)
            if checked.get("found", True):
                approval_state = _merge_delivery_result(checked, approval_state)
            await incident_store.add_event(incident_id, "approval_requested", "alert_webhook", action, approval_id)
            notice_result = await _publish_native_approval_notice(
                incident_id,
                alert,
                approval_state,
                action=action,
                risk_level=risk_level,
                config=config,
            )
            return _merge_delivery_result(approval_state, notice_result)

        record_failed = getattr(approval_async, "record_external_approval_create_failed", None)
        if callable(record_failed):
            await record_failed(
                approval_id,
                provider="feishu",
                error_type=str(native_result.get("error_type") or "feishu_error"),
                message=str(native_result.get("message") or "create approval failed"),
            )
        await incident_store.add_event(incident_id, "approval_create_failed", "alert_webhook", action, approval_id)
        return await approval_async.check_approval(approval_id)

    request_with_card = getattr(approval_async, "request_approval_with_card", None)
    approval_result = None
    if callable(request_with_card):
        approval_result = await request_with_card(
            operation_type,
            action,
            context,
            namespace,
            "alert_webhook",
            risk_level,
            incident_id=incident_id,
            config=config,
        )
        approval_id = str(approval_result.get("approval_id") or "").strip()
    else:
        approval_id = await approval_async.request_approval(
            operation_type,
            action,
            context,
            namespace,
            "alert_webhook",
            risk_level,
            incident_id=incident_id,
        )
    if not approval_id:
        approval_id = await approval_async.request_approval(
            operation_type,
            action,
            context,
            namespace,
            "alert_webhook",
            risk_level,
            incident_id=incident_id,
        )
    await incident_store.add_event(incident_id, "approval_requested", "alert_webhook", action, approval_id)
    approval_state = await approval_async.check_approval(approval_id)
    return _merge_delivery_result(approval_state, approval_result)


async def _collect_targeted_k8s_evidence(alert: Dict[str, Any], analysis: Dict[str, List[Any]], config: Dict[str, Any]) -> None:
    """优先采集 pod/workload 定向证据。"""
    del config
    namespace = alert.get("namespace")
    pod_name = alert.get("pod_name")
    workload_kind = alert.get("workload_kind")
    workload_name = alert.get("workload_name")

    commands: List[tuple[str, str]] = []
    if pod_name and namespace:
        commands.append(("pod_describe", f"kubectl describe pod {pod_name} -n {namespace}"))
        log_command = _targeted_log_command(alert)
        if log_command:
            commands.append(("pod_logs", log_command))
        commands.append(("pod_status", f"kubectl get pod {pod_name} -n {namespace} -o wide"))
    if workload_kind == "Deployment" and workload_name and namespace:
        commands.append(("workload", f"kubectl get deploy {workload_name} -n {namespace}"))

    for evidence_kind, command in commands:
        result = await _run_kubectl_command(command)
        if not result["ok"]:
            continue

        output = result["stdout"]
        if not output.strip():
            continue

        summary = _summarize_targeted_output(command, output)
        analysis["supporting_evidence"].append(
            {
                "kind": evidence_kind,
                "source": command,
                "summary": summary,
            }
        )

        lowered = output.lower()
        if evidence_kind == "pod_logs":
            if "缺少 pod 日志摘要" in analysis["missing_evidence"]:
                analysis["missing_evidence"].remove("缺少 pod 日志摘要")
            _append_unique(analysis["next_best_actions"], "检查最近 15 分钟的应用启动失败日志")

        if any(token in lowered for token in ["crashloopbackoff", "back-off", "error", "failed"]):
            _append_unique(analysis["suspected_root_causes"], "Pod/容器启动异常或持续崩溃")
        if any(token in lowered for token in ["oomkilled", "evicted"]):
            _append_unique(analysis["suspected_root_causes"], "Pod 可能受到资源限制或节点压力影响")
        if evidence_kind == "workload":
            _append_unique(analysis["next_best_actions"], "核对 Deployment 副本状态与最近变更")
            _append_unique(analysis["next_best_actions"], f"重启 deployment/{workload_name}")


async def _collect_namespace_fallback_evidence(
    alert: Dict[str, Any], analysis: Dict[str, List[Any]], config: Dict[str, Any]
) -> None:
    """保留 namespace 级 fallback 扩展点，当前任务仅要求顺序钩子存在。"""
    del alert, analysis, config


async def _persist_case_profile_for_incident(incident: Dict[str, Any]) -> None:
    """在 incident resolved 后沉淀最小 case profile。"""
    upsert_case_profile = getattr(incident_store, "upsert_case_profile", None)
    get_analysis = getattr(incident_store, "get_analysis", None)
    list_evidence = getattr(incident_store, "list_evidence", None)
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
    change_evidence = next((item for item in evidence_rows if item.get("source_type") == "audit_change"), None)
    created_at = float(incident.get("created_at") or 0.0)
    resolved_at = float(incident.get("resolved_at") or time.time())

    await upsert_case_profile(
        incident_id,
        incident_signature=f"{incident.get('alert_name', '')}|{incident.get('namespace', '')}|{likely_scope}|resolved",
        symptom_fingerprint="+".join(str(item) for item in (analysis.get("symptoms") or [])) or None,
        final_scope=likely_scope,
        final_root_cause=top_root_cause,
        effective_actions=effective_actions,
        invalid_actions=[],
        metric_delta_summary=dict(metrics_evidence.get("payload") or {}) if metrics_evidence else {},
        change_clue_summary=str(change_evidence.get("summary") or "") if change_evidence else None,
        resolution_seconds=(resolved_at - created_at) if created_at > 0 else None,
        similar_incident_ids=[],
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
            analysis = _initial_analysis()
            enriched_alert = dict(alert)
            enriched_alert["analysis"] = analysis
            await _collect_targeted_k8s_evidence(enriched_alert, analysis, config)
            await _collect_namespace_fallback_evidence(enriched_alert, analysis, config)

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
                enriched_alert["alertname"],
                enriched_alert["description"],
                enriched_alert,
            )
            await _persist_incident_analysis_context(incident_id, enriched_alert, analysis)
            await _attach_similar_case_recall(incident_id, enriched_alert, analysis)
            feishu_binding = await feishu_conversation.publish_incident_status(incident_id, enriched_alert, config)
            if feishu_binding.get("chat_id"):
                effective_feishu_binding = dict(feishu_binding)
                await incident_store.update_feishu_binding(incident_id, **feishu_binding)
                publish_summary = getattr(feishu_conversation, "publish_incident_analysis_summary", None)
                if callable(publish_summary):
                    incident = {
                        "id": incident_id,
                        "chat_id": feishu_binding.get("chat_id"),
                        "root_message_id": feishu_binding.get("root_message_id"),
                        "thread_id": feishu_binding.get("thread_id"),
                        "status_card_message_id": feishu_binding.get("status_card_message_id"),
                    }
                    summary_text = incident_analysis_summary.render_thread_summary(
                        incident,
                        enriched_alert,
                        analysis,
                        analysis.get("supporting_evidence") if isinstance(analysis, dict) else [],
                    )
                    summary_binding = await publish_summary(incident, summary_text, config)
                    if (
                        isinstance(summary_binding, dict)
                        and summary_binding.get("thread_id")
                        and summary_binding.get("thread_id") != feishu_binding.get("thread_id")
                    ):
                        effective_feishu_binding.update(
                            {
                                "root_message_id": summary_binding.get("root_message_id")
                                or feishu_binding.get("root_message_id"),
                                "thread_id": summary_binding.get("thread_id"),
                            }
                        )
                        await incident_store.update_feishu_binding(
                            incident_id,
                            chat_id=str(feishu_binding.get("chat_id") or ""),
                            root_message_id=summary_binding.get("root_message_id") or feishu_binding.get("root_message_id"),
                            thread_id=summary_binding.get("thread_id"),
                            status_card_message_id=feishu_binding.get("status_card_message_id"),
                        )
                enriched_alert["feishu_binding"] = effective_feishu_binding
                await _maybe_request_phase3_approval(incident_id, enriched_alert, analysis, config)
            processed += 1
            prompts.append(_build_triage_prompt(enriched_alert, incident_id))
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
