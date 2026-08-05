"""Connector Command owner workflow for synchronous Diagnosis reads."""

from __future__ import annotations

import json
import time

from .connector_commands import ConnectorCommands


def run_diagnosis_read(
    commands: ConnectorCommands,
    *,
    cluster_id: object,
    namespace: object,
    parameters: object,
    actor_id: str,
    reason: object,
    request_id: str,
) -> dict[str, object]:
    command = commands.queue_read(
        cluster_id=cluster_id,
        namespace=namespace,
        action="get_resource",
        parameters=parameters,
        actor_id=actor_id,
        reason=reason,
        request_id=request_id,
    )
    deadline = time.monotonic() + 15
    while command["status"] not in {
        "succeeded", "failed", "rejected", "unknown_outcome",
    } and time.monotonic() < deadline:
        time.sleep(0.1)
        command = commands.get(str(command["id"]))
    result = command.get("result")
    if command["status"] != "succeeded" or not isinstance(result, dict):
        if isinstance(result, dict):
            code = str(result.get("error_code") or command["status"])
            summary = result.get("error_message") or code
        else:
            code, summary = "read_timeout", "Connector 读取超时"
        return _failed(command, code=code, summary=summary)
    if result.get("truncated") is not False:
        return _failed(command, code="truncated_output", summary="Connector 读取结果已截断")
    if result.get("exit_code") != 0:
        return _failed(command, code="connector_read_failed", summary="Connector 读取命令失败")
    try:
        data = json.loads(str(result.get("stdout") or ""))
    except json.JSONDecodeError:
        return _failed(command, code="invalid_json", summary="Connector 读取结果不是合法 JSON")
    if not isinstance(data, dict):
        return _failed(command, code="invalid_json", summary="Connector 读取结果不是 JSON object")
    return {
        "tool_name": "run_k8s_read",
        "status": "succeeded",
        "summary": "Connector 返回了实时 Kubernetes 资源",
        "data": data,
        "evidence_refs": [{
            "ref_id": f"connector-command:{command['id']}",
            "source": "k8s",
            "cluster_id": command["cluster_id"],
            "namespace": command["namespace"],
        }],
        "audit": {"status": "succeeded", "command_id": command["id"]},
    }


def _failed(command: dict[str, object], *, code: object, summary: object) -> dict[str, object]:
    bounded_code = str(code or "connector_read_failed")[:128]
    bounded_summary = str(summary or bounded_code)[:512]
    return {
        "tool_name": "run_k8s_read", "status": "failed", "summary": bounded_summary,
        "data": {}, "evidence_refs": [],
        "audit": {"status": "failed", "command_id": command["id"], "error_code": bounded_code},
    }
