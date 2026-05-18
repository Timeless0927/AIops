"""测试飞书原生审批 OpenAPI 客户端。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


def _load_module():
    """按文件路径加载飞书原生审批模块。"""
    module_path = Path(__file__).resolve().parents[1] / "toolsets" / "feishu_native_approval.py"
    assert module_path.exists(), "toolsets/feishu_native_approval.py is required"
    spec = importlib.util.spec_from_file_location("test_feishu_native_approval_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeFeishuHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def post_json(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers or {}, "timeout": timeout})
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _approval_config() -> dict:
    return {
        "platforms": {
            "feishu": {
                "app_id": "cli_test",
                "app_secret": "app_secret",
                "approval": {
                    "enabled": True,
                    "approval_code": "approval-code",
                    "requester_open_id": "ou_requester",
                    "user_id_type": "open_id",
                    "timeout_seconds": 3,
                },
            }
        }
    }


async def _create_instance(module, http):
    create_instance = getattr(module, "create_approval_instance", None)
    assert callable(create_instance), "create_approval_instance(...) is required"
    return await create_instance(
        approval_id="ap-native-1",
        operation_type="k8s_write",
        command="kubectl rollout restart deployment/nginx -n default",
        context={
            "action_signature": "restart_deployment:prod-a:default:deployment/nginx",
            "alertname": "PodCrashLooping",
            "cluster": "prod-a",
        },
        namespace="default",
        requester_open_id="ou_requester",
        risk_level="low",
        config=_approval_config(),
        http_client=http,
    )


@pytest.mark.asyncio
async def test_create_instance_success_returns_external_fields_and_payload(**_kwargs) -> None:
    """创建审批实例成功后应返回可写入本地 approval 的外部字段。"""
    module = _load_module()
    http = FakeFeishuHTTP(
        [
            {"code": 0, "tenant_access_token": "tenant-token"},
            {
                "code": 0,
                "data": {
                    "instance_code": "INST-001",
                    "approve_url": "https://approval.feishu.cn/approval/INST-001",
                },
            },
        ]
    )

    result = await _create_instance(module, http)

    assert result["ok"] is True
    assert result["external_provider"] == "feishu"
    assert result["external_uuid"] == "ap-native-1"
    assert result["external_instance_code"] == "INST-001"
    assert result["external_url"] == "https://approval.feishu.cn/approval/INST-001"
    assert result["external_status"] in {"PENDING", "pending"}
    assert http.calls[0]["url"].endswith("/auth/v3/tenant_access_token/internal")
    assert http.calls[1]["url"].endswith("/approval/v4/instances")
    assert http.calls[1]["headers"]["Authorization"] == "Bearer tenant-token"
    payload = http.calls[1]["json"]
    assert payload["approval_code"] == "approval-code"
    assert payload["uuid"] == "ap-native-1"
    assert payload["user_id"] == "ou_requester"
    form = json.loads(payload["form"]) if isinstance(payload.get("form"), str) else payload.get("form")
    assert "kubectl rollout restart deployment/nginx" in json.dumps(form, ensure_ascii=False)
    assert "low" in json.dumps(form, ensure_ascii=False)


@pytest.mark.parametrize(
    ("responses", "error_type", "message_part"),
    [
        ([{"code": 99991663, "msg": "invalid app_secret"}], "token_error", "tenant_access_token"),
        (
            [
                {"code": 0, "tenant_access_token": "tenant-token"},
                {"code": 1254001, "msg": "approval_code not found"},
            ],
            "feishu_error",
            "approval_code not found",
        ),
        ([TimeoutError("request timed out")], "http_timeout", "timeout"),
        ([{"code": 0, "tenant_access_token": "tenant-token"}, ValueError("response is not JSON")], "invalid_json", "json"),
    ],
)
@pytest.mark.asyncio
async def test_create_instance_errors_are_explicit(responses, error_type, message_part, **_kwargs) -> None:
    """飞书错误、token 失败、超时和非 JSON 响应应给出明确错误分类。"""
    module = _load_module()
    http = FakeFeishuHTTP(responses)

    result = await _create_instance(module, http)

    assert result["ok"] is False
    assert result["error_type"] == error_type
    assert message_part.lower() in str(result["message"]).lower()
