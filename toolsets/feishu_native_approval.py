"""飞书原生审批 OpenAPI 封装。"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any


DEFAULT_OPENAPI_BASE_URL = "https://open.feishu.cn/open-apis"


class _AiohttpJSONClient:
    async def post_json(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | int | None = None,
    ) -> dict[str, Any]:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=json,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout or 10),
            ) as response:
                return await response.json()

    async def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float | int | None = None,
    ) -> dict[str, Any]:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout or 10),
            ) as response:
                return await response.json()


def _feishu_config(config: dict[str, Any] | None) -> dict[str, Any]:
    root = config if isinstance(config, dict) else {}
    platforms = root.get("platforms") if isinstance(root.get("platforms"), dict) else {}
    feishu = platforms.get("feishu") if isinstance(platforms.get("feishu"), dict) else {}
    return feishu


def _approval_config(config: dict[str, Any] | None) -> dict[str, Any]:
    feishu = _feishu_config(config)
    approval = feishu.get("approval") if isinstance(feishu.get("approval"), dict) else {}
    return approval


def _effective_approval_config(config: dict[str, Any] | None, approval_code: str) -> dict[str, Any]:
    """Merge root approval config with optional approval_code-specific definition config."""
    approval = _approval_config(config)
    effective = dict(approval)
    definitions = approval.get("definitions") if isinstance(approval.get("definitions"), dict) else {}
    definition = definitions.get(approval_code) if isinstance(definitions.get(approval_code), dict) else {}
    effective.update(definition)
    return effective


def _base_url(config: dict[str, Any] | None) -> str:
    feishu = _feishu_config(config)
    approval = _approval_config(config)
    value = approval.get("openapi_base_url") or feishu.get("openapi_base_url") or DEFAULT_OPENAPI_BASE_URL
    return str(value).rstrip("/")


def _timeout(config: dict[str, Any] | None) -> float:
    approval = _approval_config(config)
    try:
        return float(approval.get("timeout_seconds") or 10)
    except (TypeError, ValueError):
        return 10.0


def _error(error_type: str, message: str, **extra: Any) -> dict[str, Any]:
    result = {"ok": False, "error_type": error_type, "message": message}
    result.update(extra)
    return result


def _text(value: Any, default: str = "") -> str:
    normalized = str(value if value is not None else default).strip()
    return normalized if normalized else default


def _field_config(approval: dict[str, Any], name: str) -> tuple[str, str | None]:
    field_value = None
    for group_name in ("fields", "mapped_fields", "legacy_fields"):
        fields = approval.get(group_name) if isinstance(approval.get(group_name), dict) else {}
        if name in fields:
            field_value = fields.get(name)
            break
    if isinstance(field_value, dict):
        field_id = _text(field_value.get("id") or field_value.get("field_id") or field_value.get("control_id"))
        field_type = _text(field_value.get("type")) or None
        return field_id, field_type

    field_id = _text(
        field_value
        or approval.get(f"{name}_field")
        or approval.get(f"{name}_field_id")
        or approval.get(f"{name}_control_id")
    )
    field_type = _text(approval.get(f"{name}_field_type") or approval.get(f"{name}_type")) or None
    return field_id, field_type


def _require_field(approval: dict[str, Any], name: str) -> tuple[str, str | None, dict[str, Any] | None]:
    field_id, field_type = _field_config(approval, name)
    if not field_id:
        return "", None, _error(
            "config_error",
            f"approval form field config is required: {name}",
            missing_field=name,
        )
    return field_id, field_type, None


def _require_field_type(
    field_name: str,
    field_type: str | None,
    allowed_types: set[str],
) -> dict[str, Any] | None:
    if not field_type or field_type in allowed_types:
        return None
    return _error(
        "config_error",
        f"approval form field type mismatch: {field_name} expects {sorted(allowed_types)}, got {field_type}",
        field=field_name,
        expected_types=sorted(allowed_types),
        actual_type=field_type,
    )


def _first_text(*values: Any, default: str = "-") -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return default


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _nested_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _remediation_action(context: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(context, dict):
        return {}
    return _nested_dict(context.get("remediation_action"))


def _approval_summary(
    *,
    approval_id: str,
    command: str,
    namespace: str | None,
    context: dict[str, Any] | None,
) -> str:
    del approval_id
    action = _remediation_action(context)
    source = _nested_dict(action.get("source"))
    alertname = _first_text(
        source.get("alertname"),
        context.get("alertname") if isinstance(context, dict) else None,
        default="未知告警",
    )
    cluster = _first_text(
        action.get("cluster"),
        context.get("cluster") if isinstance(context, dict) else None,
        default="-",
    )
    normalized_namespace = _first_text(action.get("namespace"), namespace, default="-")
    summary = f"{alertname} {normalized_namespace}/{cluster}: {command}"
    if len(summary) <= 120:
        return summary
    return f"{summary[:117]}..."


def _approval_detail(
    *,
    approval_id: str,
    operation_type: str,
    command: str,
    namespace: str | None,
    risk_level: str,
    context: dict[str, Any] | None,
) -> str:
    action = _remediation_action(context)
    source = _nested_dict(action.get("source"))
    risk = _nested_dict(action.get("risk"))
    parameters = action.get("parameters")
    incident_id = _first_text(
        source.get("incident_id"),
        context.get("incident_id") if isinstance(context, dict) else None,
        default="-",
    )
    alertname = _first_text(
        source.get("alertname"),
        context.get("alertname") if isinstance(context, dict) else None,
        default="-",
    )
    cluster = _first_text(
        action.get("cluster"),
        context.get("cluster") if isinstance(context, dict) else None,
        default="-",
    )
    normalized_namespace = _first_text(
        action.get("namespace"),
        context.get("namespace") if isinstance(context, dict) else None,
        namespace,
        default="-",
    )
    action_type = _first_text(action.get("action_type"), operation_type, default="-")
    action_signature = _first_text(
        action.get("action_signature"),
        context.get("action_signature") if isinstance(context, dict) else None,
        default="-",
    )
    analysis_action = _first_text(source.get("analysis_action"), command, default="-")
    reason = _first_text(
        context.get("non_executable_reason") if isinstance(context, dict) else None,
        context.get("source") if isinstance(context, dict) else None,
        default="alert_webhook 自动触发",
    )
    resolved_risk_level = _first_text(risk.get("risk_level"), risk_level, default="-")
    resolved_operation_type = _first_text(risk.get("operation_type"), operation_type, default="-")
    facts_source = _first_text(
        context.get("source") if isinstance(context, dict) else None,
        default="alert_webhook/remediation_plan",
    )

    lines = [
        f"本地审批 ID: {approval_id}",
        f"Incident ID: {incident_id}",
        f"Alertname: {alertname}",
        f"Namespace: {normalized_namespace}",
        f"Cluster: {cluster}",
        f"风险等级: {resolved_risk_level}",
        f"操作类型: {resolved_operation_type}",
        f"建议动作/命令: {command}",
        f"动作类型: {action_type}",
        f"动作签名: {action_signature}",
        f"触发原因: {reason}",
        "执行提示: 审批通过后，AIOps 将按本地审批上下文执行建议动作；审批拒绝则不会执行。",
        "回滚提示: 如执行后健康检查失败，请按变更记录和 Kubernetes rollout history/scale 参数执行回滚。",
        f"AIOps 本地事实源: {facts_source}",
    ]
    if analysis_action != command:
        lines.insert(8, f"分析建议: {analysis_action}")
    if parameters is not None:
        lines.append(f"动作参数: {_compact_json(parameters)}")
    if context:
        lines.append(f"原始上下文: {_compact_json(context)}")
    return "\n".join(lines)


def _form_entry(field_id: str, field_type: str | None, value: str, default_type: str) -> dict[str, str]:
    return {"id": field_id, "type": field_type or default_type, "value": value}


_MAPPED_FIELD_ALIASES = {
    "source": ("source", "request_source", "actual_source", "actual_request_source"),
    "incident_id": ("incident_id", "incident"),
    "risk_level": ("risk_level", "risk"),
    "command": ("command", "operation_command", "operation"),
    "namespace": ("namespace", "k8s_namespace"),
    "reason": ("reason", "trigger_reason", "cause"),
}


def _require_mapped_field(approval: dict[str, Any], name: str) -> tuple[str, str | None, dict[str, Any] | None]:
    for candidate in _MAPPED_FIELD_ALIASES.get(name, (name,)):
        field_id, field_type = _field_config(approval, candidate)
        if field_id:
            return field_id, field_type, None
    return "", None, _error(
        "config_error",
        f"approval form field config is required: {name}",
        missing_field=name,
    )


def _mapped_field_values(
    *,
    approval_id: str,
    operation_type: str,
    command: str,
    namespace: str | None,
    risk_level: str,
    context: dict[str, Any] | None,
) -> dict[str, str]:
    del approval_id, operation_type
    action = _remediation_action(context)
    source = _nested_dict(action.get("source"))
    risk = _nested_dict(action.get("risk"))
    alertname = _first_text(
        source.get("alertname"),
        context.get("alertname") if isinstance(context, dict) else None,
        default="-",
    )
    request_source = _first_text(
        context.get("request_source") if isinstance(context, dict) else None,
        context.get("source") if isinstance(context, dict) else None,
        source.get("source"),
        default="alert_webhook",
    )
    incident_id = _first_text(
        source.get("incident_id"),
        context.get("incident_id") if isinstance(context, dict) else None,
        default="-",
    )
    normalized_namespace = _first_text(
        action.get("namespace"),
        context.get("namespace") if isinstance(context, dict) else None,
        namespace,
        default="-",
    )
    resolved_risk_level = _first_text(risk.get("risk_level"), risk_level, default="-")
    reason = _first_text(
        context.get("trigger_reason") if isinstance(context, dict) else None,
        context.get("non_executable_reason") if isinstance(context, dict) else None,
        source.get("reason"),
        f"{alertname} 自动触发" if alertname != "-" else None,
        default=f"{request_source} 自动触发",
    )
    return {
        "source": request_source,
        "incident_id": incident_id,
        "risk_level": resolved_risk_level,
        "command": _first_text(command, default="-"),
        "namespace": normalized_namespace,
        "reason": reason,
    }


def _normalize_id_list(value: Any) -> list[str]:
    if isinstance(value, list):
        candidates = value
    elif isinstance(value, str):
        candidates = value.split(",")
    elif value is None:
        candidates = []
    else:
        candidates = [value]
    return [str(item).strip() for item in candidates if str(item).strip()]


async def _post_json(
    http_client: Any,
    url: str,
    *,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
    timeout: float,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        response = await http_client.post_json(url, json=payload, headers=headers or {}, timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError) as exc:
        return None, _error("http_timeout", f"request timeout: {exc}")
    except ValueError as exc:
        return None, _error("invalid_json", f"response json parse failed: {exc}")
    except Exception as exc:
        return None, _error("http_error", str(exc))
    if not isinstance(response, dict):
        return None, _error("invalid_json", "response is not JSON object")
    return response, None


async def _get_json(
    http_client: Any,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    getter = getattr(http_client, "get_json", None)
    if not callable(getter):
        return await _post_json(http_client, url, payload={}, headers=headers, timeout=timeout)
    try:
        response = await getter(url, headers=headers or {}, timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError) as exc:
        return None, _error("http_timeout", f"request timeout: {exc}")
    except ValueError as exc:
        return None, _error("invalid_json", f"response json parse failed: {exc}")
    except Exception as exc:
        return None, _error("http_error", str(exc))
    if not isinstance(response, dict):
        return None, _error("invalid_json", "response is not JSON object")
    return response, None


async def _tenant_access_token(
    config: dict[str, Any] | None,
    *,
    http_client: Any,
    injected_client: bool,
) -> tuple[str | None, dict[str, Any] | None]:
    if not injected_client:
        env_token = os.getenv("FEISHU_TENANT_ACCESS_TOKEN")
        if env_token:
            return env_token.strip(), None

    feishu = _feishu_config(config)
    app_id = os.getenv("FEISHU_APP_ID") or str(feishu.get("app_id") or "")
    app_secret = os.getenv("FEISHU_APP_SECRET") or str(feishu.get("app_secret") or "")
    if not app_id or not app_secret or app_id.startswith("${") or app_secret.startswith("${"):
        return None, _error("token_error", "tenant_access_token requires app_id and app_secret")

    response, error = await _post_json(
        http_client,
        f"{_base_url(config)}/auth/v3/tenant_access_token/internal",
        payload={"app_id": app_id, "app_secret": app_secret},
        timeout=_timeout(config),
    )
    if error:
        return None, error
    if response.get("code") != 0:
        return None, _error(
            "token_error",
            f"tenant_access_token failed: {response.get('msg') or response.get('message') or response.get('code')}",
            raw=response,
        )
    token = response.get("tenant_access_token") or (response.get("data") or {}).get("tenant_access_token")
    if not token:
        return None, _error("token_error", "tenant_access_token missing in response", raw=response)
    return str(token), None


def _approval_form(
    *,
    approval: dict[str, Any],
    approval_id: str,
    operation_type: str,
    command: str,
    namespace: str | None,
    risk_level: str,
    context: dict[str, Any] | None,
) -> tuple[str | None, dict[str, Any] | None]:
    mode = _text(approval.get("mode") or "summary_detail")
    if mode == "summary_detail":
        summary_field, summary_type, summary_error = _require_field(approval, "summary")
        if summary_error:
            return None, summary_error
        detail_field, detail_type, detail_error = _require_field(approval, "detail")
        if detail_error:
            return None, detail_error
        type_error = _require_field_type("summary", summary_type, {"input"})
        if type_error:
            return None, type_error
        type_error = _require_field_type("detail", detail_type, {"textarea"})
        if type_error:
            return None, type_error
        form = [
            _form_entry(
                summary_field,
                summary_type,
                _approval_summary(
                    approval_id=approval_id,
                    command=command,
                    namespace=namespace,
                    context=context,
                ),
                "input",
            ),
            _form_entry(
                detail_field,
                detail_type,
                _approval_detail(
                    approval_id=approval_id,
                    operation_type=operation_type,
                    command=command,
                    namespace=namespace,
                    risk_level=risk_level,
                    context=context,
                ),
                "textarea",
            ),
        ]
        return json.dumps(form, ensure_ascii=False), None

    if mode == "single_text":
        text_field, text_type, text_error = _require_field(approval, "text")
        if text_error:
            return None, text_error
        type_error = _require_field_type("text", text_type, {"input", "textarea"})
        if type_error:
            return None, type_error
        summary = _approval_summary(
            approval_id=approval_id,
            command=command,
            namespace=namespace,
            context=context,
        )
        detail = _approval_detail(
            approval_id=approval_id,
            operation_type=operation_type,
            command=command,
            namespace=namespace,
            risk_level=risk_level,
            context=context,
        )
        form = [_form_entry(text_field, text_type, f"{summary}\n\n{detail}", "textarea")]
        return json.dumps(form, ensure_ascii=False), None

    if mode in {"mapped_fields", "legacy_fields"}:
        values = _mapped_field_values(
            approval_id=approval_id,
            operation_type=operation_type,
            command=command,
            namespace=namespace,
            risk_level=risk_level,
            context=context,
        )
        form = []
        for field_name in ("source", "incident_id", "risk_level", "command", "namespace", "reason"):
            field_id, field_type, field_error = _require_mapped_field(approval, field_name)
            if field_error:
                return None, field_error
            type_error = _require_field_type(field_name, field_type, {"input", "textarea"})
            if type_error:
                return None, type_error
            form.append(_form_entry(field_id, field_type, values[field_name], "input"))
        return json.dumps(form, ensure_ascii=False), None

    return None, _error(
        "config_error",
        f"unsupported approval form mode: {mode}",
        mode=mode,
    )


async def create_approval_instance(
    *,
    approval_id: str,
    operation_type: str,
    command: str,
    context: dict[str, Any] | None,
    namespace: str | None,
    requester_open_id: str | None = None,
    risk_level: str = "standard",
    config: dict[str, Any] | None = None,
    http_client: Any | None = None,
) -> dict[str, Any]:
    """创建飞书原生审批实例。"""
    root_approval = _approval_config(config)
    approval_code = str(root_approval.get("approval_code") or "").strip()
    if not approval_code:
        return _error("config_error", "approval_code is required")
    approval = _effective_approval_config(config, approval_code)

    form, form_error = _approval_form(
        approval=approval,
        approval_id=approval_id,
        operation_type=operation_type,
        command=command,
        namespace=namespace,
        risk_level=risk_level,
        context=context,
    )
    if form_error:
        return form_error

    injected_client = http_client is not None
    client = http_client or _AiohttpJSONClient()
    token, token_error = await _tenant_access_token(config, http_client=client, injected_client=injected_client)
    if token_error:
        return token_error

    user_id_type = str(approval.get("user_id_type") or "open_id").strip() or "open_id"
    requester_key = "open_id" if user_id_type == "open_id" else "user_id"
    requester_candidates = [
        approval.get(f"requester_{requester_key}"),
        approval.get("requester_id"),
    ]
    if requester_key == "open_id":
        requester_candidates.extend(
            [
                approval.get("requester_open_id"),
                requester_open_id,
                os.getenv("FEISHU_APPROVAL_REQUESTER_OPEN_ID"),
            ]
        )
    else:
        requester_candidates.append(approval.get("requester_user_id"))
    requester = _first_text(*requester_candidates, default="")
    if not requester:
        return _error("config_error", f"requester_{requester_key} is required")

    payload = {
        "approval_code": approval_code,
        "uuid": approval_id,
        requester_key: requester,
        "user_id_type": user_id_type,
        "form": form,
    }
    approver_node_key = _text(
        approval.get("approver_node_key")
        or approval.get("node_approver_key")
        or approval.get("approver_node_id")
        or approval.get("node_id")
    )
    approvers_value = (
        approval.get("approver_open_ids")
        if user_id_type == "open_id"
        else approval.get("approver_user_ids")
    )
    if approvers_value is None:
        approvers_value = (
            approval.get("approver_open_id")
            if user_id_type == "open_id"
            else approval.get("approver_user_id")
        )
    if approvers_value is None:
        approvers_value = approval.get("approver_ids") or approval.get("approvers")
    approvers = _normalize_id_list(approvers_value)
    if approver_node_key and approvers:
        approver_field = (
            "node_approver_open_id_list"
            if user_id_type == "open_id"
            else "node_approver_user_id_list"
        )
        payload[approver_field] = [{"key": approver_node_key, "value": approvers}]
    response, request_error = await _post_json(
        client,
        f"{_base_url(config)}/approval/v4/instances",
        payload=payload,
        headers={"Authorization": f"Bearer {token}"},
        timeout=_timeout(config),
    )
    if request_error:
        return request_error
    if response.get("code") != 0:
        return _error(
            "feishu_error",
            str(response.get("msg") or response.get("message") or response.get("code")),
            raw=response,
        )

    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    instance_code = str(data.get("instance_code") or data.get("instanceCode") or "").strip()
    if not instance_code:
        return _error("feishu_error", "instance_code missing in response", raw=response)
    return {
        "ok": True,
        "external_provider": "feishu",
        "external_uuid": approval_id,
        "external_approval_code": approval_code,
        "external_instance_code": instance_code,
        "external_status": str(data.get("status") or "PENDING"),
        "external_url": data.get("approve_url") or data.get("approval_url") or data.get("url"),
    }


async def query_approval_instance(
    *,
    instance_code: str,
    config: dict[str, Any] | None = None,
    http_client: Any | None = None,
) -> dict[str, Any]:
    """查询飞书原生审批实例状态。"""
    injected_client = http_client is not None
    client = http_client or _AiohttpJSONClient()
    token, token_error = await _tenant_access_token(config, http_client=client, injected_client=injected_client)
    if token_error:
        return token_error

    normalized = str(instance_code or "").strip()
    if not normalized:
        return _error("config_error", "instance_code is required")
    response, request_error = await _get_json(
        client,
        f"{_base_url(config)}/approval/v4/instances/{normalized}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=_timeout(config),
    )
    if request_error:
        return request_error
    if response.get("code") != 0:
        return _error(
            "feishu_error",
            str(response.get("msg") or response.get("message") or response.get("code")),
            raw=response,
        )

    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    status = data.get("status") or data.get("instance_status") or data.get("approval_status")
    return {
        "ok": True,
        "external_provider": "feishu",
        "external_instance_code": normalized,
        "external_status": str(status or "PENDING"),
        "external_url": data.get("approve_url") or data.get("approval_url") or data.get("url"),
    }
