"""Diagnosis knowledge-only Chat seam."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from diagnosis_service import service_main
from diagnosis_service.diagnosis_provider import ProviderResult
from diagnosis_service.knowledge_chat import KnowledgeChatError, answer_knowledge_chat
from diagnosis_service.model_provider import ModelProviderError


async def test_knowledge_chat_answers_without_tools() -> None:
    class Provider:
        async def chat_with_tools(self, messages, tools):
            assert tools == []
            assert messages[-1] == {"role": "user", "content": "什么是滚动发布？"}
            return ProviderResult(
                {"role": "assistant", "content": "滚动发布会逐步替换旧实例。"},
                [],
                "stop",
                {},
            )

    answer = await answer_knowledge_chat(
        [{"role": "user", "content": "什么是滚动发布？"}],
        Provider(),
    )

    assert answer == "滚动发布会逐步替换旧实例。"


async def test_knowledge_chat_rejects_model_tool_requests() -> None:
    class Provider:
        async def chat_with_tools(self, _messages, _tools):
            return ProviderResult(
                {"role": "assistant", "content": None},
                [object()],
                "tool_calls",
                {},
            )

    with pytest.raises(KnowledgeChatError) as rejected:
        await answer_knowledge_chat([{"role": "user", "content": "检查线上 Pod"}], Provider())
    assert rejected.value.code == "invalid_model_response"


def test_internal_knowledge_chat_http_uses_configured_provider(monkeypatch) -> None:
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
            f"http://127.0.0.1:{server.server_address[1]}/chat/knowledge",
            data=json.dumps({"messages": [{"role": "user", "content": "解释 Service"}]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            payload = json.loads(response.read())
        assert response.status == 200
        assert payload["answer"] == "这是知识回答。"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_internal_knowledge_chat_reports_unavailable_provider(monkeypatch) -> None:
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
            f"http://127.0.0.1:{server.server_address[1]}/chat/knowledge",
            data=b'{"messages":[{"role":"user","content":"hello"}]}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as response:
            urllib.request.urlopen(request, timeout=3)
        payload = json.loads(response.value.read())
        assert response.value.code == 503
        assert payload["error"] == {"code": "not_configured", "message": "Model Provider is not ready"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
