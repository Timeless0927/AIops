"""Diagnosis governed knowledge Chat seam."""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from aiops.contracts.governed_tools import default_capability_snapshot
from diagnosis_service import service_main
from diagnosis_service.chat_loop_checkpoints import ChatLoopCheckpoints
from diagnosis_service.diagnosis_provider import ProviderResult, ToolCall
from diagnosis_service.governed_chat import GovernedChatError, answer_governed_chat
from diagnosis_service.model_provider import ModelProviderError


def test_knowledge_chat_answers_without_tools(tmp_path: Path) -> None:
    class Provider:
        async def chat_with_tools(self, messages, tools):
            assert tools == []
            assert messages[-1] == {"role": "user", "content": "什么是滚动发布？"}
            return ProviderResult(
                {"role": "assistant", "content": "滚动发布会逐步替换旧实例。"}, [], "stop", {},
            )

    result = asyncio.run(answer_governed_chat(
        {"request_id": "knowledge-1", "messages": [{"role": "user", "content": "什么是滚动发布？"}]},
        provider=Provider(), checkpoints=ChatLoopCheckpoints(tmp_path / "diagnosis.db"),
        adapters={},
    ))
    assert result["answer"] == "滚动发布会逐步替换旧实例。"
    assert result["mode"] == "knowledge"


def test_knowledge_chat_rejects_model_tool_requests(tmp_path: Path) -> None:
    class Provider:
        async def chat_with_tools(self, _messages, _tools):
            return ProviderResult(
                {"role": "assistant", "content": None},
                [ToolCall("call-1", "query_metrics", {})], "tool_calls", {},
            )

    with pytest.raises(GovernedChatError) as rejected:
        asyncio.run(answer_governed_chat(
            {"request_id": "knowledge-tools", "messages": [{"role": "user", "content": "检查线上 Pod"}]},
            provider=Provider(), checkpoints=ChatLoopCheckpoints(tmp_path / "diagnosis.db"),
            adapters={},
        ))
    assert rejected.value.code == "invalid_model_response"


def test_internal_knowledge_chat_http_uses_configured_provider(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))

    class Provider:
        async def chat_with_tools(self, _messages, tools):
            assert tools == []
            return ProviderResult({"role": "assistant", "content": "这是知识回答。"}, [], "stop", {})

    class Runtime:
        @staticmethod
        def resolve_provider():
            return Provider()

    monkeypatch.setattr(service_main, "_diagnosis_runtime", lambda: Runtime())
    monkeypatch.setattr(service_main, "enforce_internal_auth", lambda *_args, **_kwargs: {"service": "gateway"})
    server = ThreadingHTTPServer(("127.0.0.1", 0), service_main.DiagnosisServiceHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/chat",
            data=json.dumps({"request_id": "http-knowledge", "messages": [{"role": "user", "content": "解释 Service"}]}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            payload = json.loads(response.read())
        assert response.status == 200
        assert payload["result"]["answer"] == "这是知识回答。"
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_internal_knowledge_chat_reports_unavailable_provider(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))

    class Runtime:
        @staticmethod
        def resolve_provider():
            raise ModelProviderError("not_configured", "private configuration detail")

    monkeypatch.setattr(service_main, "_diagnosis_runtime", lambda: Runtime())
    monkeypatch.setattr(service_main, "enforce_internal_auth", lambda *_args, **_kwargs: {"service": "gateway"})
    server = ThreadingHTTPServer(("127.0.0.1", 0), service_main.DiagnosisServiceHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/chat",
            data=b'{"request_id":"http-unavailable","messages":[{"role":"user","content":"hello"}]}',
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as response:
            urllib.request.urlopen(request, timeout=3)
        payload = json.loads(response.value.read())
        assert response.value.code == 503
        assert payload["error"] == {"code": "not_configured", "message": "Model Provider is not ready"}
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_internal_environment_chat_uses_fake_model_and_mcp_http_contract(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))

    class Provider:
        turn = 0

        async def chat_with_tools(self, _messages, tools):
            self.turn += 1
            assert tools
            if self.turn == 1:
                call = ToolCall("call-1", "query_metrics", {"deployment_target_id": "target-1"})
                return ProviderResult(
                    {"role": "assistant", "content": None, "tool_calls": [{
                        "id": call.id, "type": "function",
                        "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                    }]}, [call], "tool_calls", {},
                )
            return ProviderResult({
                "role": "assistant",
                "content": json.dumps({
                    "root_cause_candidates": [{
                        "cause": "checkout 当前错误率为 42%。", "confidence": 0.8,
                        "evidence_refs": ["evidence:metrics:1"],
                    }],
                    "recommended_actions": [],
                    "confidence": {"score": 0.8, "level": "high"},
                }, ensure_ascii=False),
            }, [], "stop", {})

    provider = Provider()

    class Runtime:
        @staticmethod
        def resolve_provider():
            return provider

    async def metrics(_args):
        return {
            "status": "succeeded", "summary": "error_rate=0.42", "data": {"error_rate": 0.42},
            "evidence_refs": [{"ref_id": "evidence:metrics:1"}],
        }

    monkeypatch.setattr(service_main, "_diagnosis_runtime", lambda: Runtime())
    monkeypatch.setattr(service_main, "_metrics_adapter", metrics)
    monkeypatch.setattr(service_main, "enforce_internal_auth", lambda *_args, **_kwargs: {"service": "gateway"})
    server = ThreadingHTTPServer(("127.0.0.1", 0), service_main.DiagnosisServiceHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    scope = {
        "selection": {"deployment_target_id": "target-1"},
        "resources": [{
            "deployment_target_id": "target-1", "cluster_id": "cluster-1", "namespace": "shop",
            "service_id": "service-1", "service_name": "checkout", "workload_kind": "Deployment",
            "workload_name": "checkout-api",
        }],
        "time_range": {"type": "relative", "value": "30m"}, "revision": "a" * 64,
    }
    capabilities = default_capability_snapshot()
    for name, capability in capabilities.items():
        capability.update({
            "integration_id": f"mcp-{name}", "integration_revision": f"mcp-revision:{name}",
        })
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/chat",
            data=json.dumps({
                "request_id": "http-environment",
                "messages": [{"role": "user", "content": "checkout 现在错误率高吗？"}],
                "scope": scope, "capabilities": capabilities,
            }).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            payload = json.loads(response.read())
        assert response.status == 200
        assert payload["result"]["evidence_references"] == ["evidence:metrics:1"]
        assert payload["result"]["tool_activity"][0]["authorized_scope"]["deployment_target_id"] == "target-1"
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
