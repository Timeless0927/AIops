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
    approval_id: str,
    operation_type: str,
    command: str,
    namespace: str | None,
    risk_level: str,
    context: dict[str, Any] | None,
) -> str:
    form = [
        {"id": "approval_id", "type": "input", "value": approval_id},
        {"id": "operation_type", "type": "input", "value": operation_type},
        {"id": "command", "type": "input", "value": command},
        {"id": "namespace", "type": "input", "value": namespace or ""},
        {"id": "risk_level", "type": "input", "value": risk_level},
        {"id": "context", "type": "textarea", "value": json.dumps(context or {}, ensure_ascii=False)},
    ]
    return json.dumps(form, ensure_ascii=False)


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
    approval = _approval_config(config)
    approval_code = str(approval.get("approval_code") or "").strip()
    if not approval_code:
        return _error("config_error", "approval_code is required")

    injected_client = http_client is not None
    client = http_client or _AiohttpJSONClient()
    token, token_error = await _tenant_access_token(config, http_client=client, injected_client=injected_client)
    if token_error:
        return token_error

    requester = (
        requester_open_id
        or approval.get("requester_open_id")
        or approval.get("requester_user_id")
        or os.getenv("FEISHU_APPROVAL_REQUESTER_OPEN_ID")
    )
    requester = str(requester or "").strip()
    if not requester:
        return _error("config_error", "requester_open_id is required")

    payload = {
        "approval_code": approval_code,
        "uuid": approval_id,
        "user_id": requester,
        "user_id_type": str(approval.get("user_id_type") or "open_id"),
        "form": _approval_form(
            approval_id=approval_id,
            operation_type=operation_type,
            command=command,
            namespace=namespace,
            risk_level=risk_level,
            context=context,
        ),
    }
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
