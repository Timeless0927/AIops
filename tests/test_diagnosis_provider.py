"""Tests for hermes.diagnosis_provider (ADR-0003 child 1).

Strategy B (pure module logic): no HTTP server, fake provider via ScriptedProvider /
urllib monkeypatch, outgress log via caplog. conftest.py drives async tests with
asyncio.run — do not mark @pytest.mark.asyncio.
"""

from __future__ import annotations

import importlib
import json
import logging
import urllib.error

import pytest

import hermes.diagnosis_provider as dp


# --- helpers ----------------------------------------------------------------

def _resp_stop(content: str = "root cause: x") -> dict:
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _resp_tool_call(name: str = "query_metrics", tool_id: str = "call_1") -> dict:
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": tool_id,
                            "type": "function",
                            "function": {"name": name, "arguments": '{"service":"payment-api"}'},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3},
    }


# --- chat_with_tools via ScriptedProvider -----------------------------------

async def test_chat_with_tools_single_turn_final():
    provider = dp.ScriptedProvider([_resp_stop()])
    result = await provider.chat_with_tools([{"role": "user", "content": "diagnose"}], [])
    assert result.finish_reason == "stop"
    assert result.message["content"] == "root cause: x"
    assert result.usage["prompt_tokens"] == 10
    assert result.tool_calls == []


async def test_chat_with_tools_returns_tool_call_for_reinject():
    provider = dp.ScriptedProvider([_resp_tool_call()])
    result = await provider.chat_with_tools([{"role": "user", "content": "diagnose"}], [])
    assert result.finish_reason == "tool_calls"
    assert len(result.tool_calls) == 1
    call = result.tool_calls[0]
    assert call.id == "call_1"
    assert call.name == "query_metrics"
    # arguments 是已 parse 的 dict,不是原始字符串
    assert call.arguments == {"service": "payment-api"}


async def test_scripted_provider_records_history():
    provider = dp.ScriptedProvider([_resp_tool_call(), _resp_stop("done")])
    msgs = [{"role": "user", "content": "diagnose"}]
    await provider.chat_with_tools(msgs, [])
    # child 2 回灌 tool result 后再调
    msgs2 = msgs + [{"role": "tool", "tool_call_id": "call_1", "content": "ok"}]
    await provider.chat_with_tools(msgs2, [])
    assert len(provider.messages_history) == 2
    assert provider.messages_history[0][0]["role"] == "user"
    assert provider.messages_history[1][1]["role"] == "tool"


# --- ProviderUnavailable on network failure/chained -------------------------

async def test_provider_unavailable_on_timeout(monkeypatch):
    config = dp.ProviderConfig(
        base_url="http://model-service.default.svc.cluster.local/v1",
        api_key="sk-test-1234",
        model="gpt-5.4",
        timeout_s=0.1,
    )

    def _boom(*args, **kwargs):
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr(dp.request, "urlopen", _boom)

    with pytest.raises(dp.ProviderUnavailable) as exc_info:
        await dp.chat_with_tools(config, [{"role": "user", "content": "x"}], [])
    assert exc_info.value.code == dp.PROVIDER_UNAVAILABLE
    # chained from the low-level error
    assert isinstance(exc_info.value.__cause__, urllib.error.URLError)


# --- ProviderConfig.chat_with_tools bound method (ADR-0003 live-cluster seam) ----

async def test_provider_config_chat_with_tools_bound_method_matches_object_surface(monkeypatch):
    """ProviderConfig must expose chat_with_tools as a bound method so that
    _resolve_diagnosis_provider() (which returns a ProviderConfig) works as the
    ``provider`` arg of run_diagnosis_session, whose loop calls
    ``provider.chat_with_tools(messages, tools)``. This is the seam the unit tests
    missed by injecting ScriptedProvider; the live cluster hit
    "'ProviderConfig' object has no attribute 'chat_with_tools'"."""
    config = dp.ProviderConfig(
        base_url="https://api.deepseek.example/v1",
        api_key="sk-test-1234",
        model="deepseek-v4-flash",
        timeout_s=3.0,
    )

    captured: dict[str, Any] = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def read(self):
            return json.dumps({
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": '{"root_cause_candidates":[]}'},
                }],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3},
            }).encode("utf-8")

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["auth"] = req.headers.get("Authorization")
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(dp.request, "urlopen", _fake_urlopen)

    # call the BOUND method, exactly as run_diagnosis_session does
    result = await config.chat_with_tools([{"role": "user", "content": "diagnose"}], [])

    assert isinstance(result, dp.ProviderResult)
    assert result.finish_reason == "stop"
    assert result.usage["prompt_tokens"] == 12
    # request hit the right endpoint with the configured model + bearer key
    assert captured["url"].endswith("/chat/completions")
    assert captured["auth"] == "Bearer sk-test-1234"
    assert captured["body"]["model"] == "deepseek-v4-flash"
    assert captured["timeout"] == 3.0


# --- outgress log -----------------------------------------------------------

def test_load_from_env_outgress_log_external(monkeypatch, caplog):
    monkeypatch.setenv("AIOPS_MODEL_BASE_URL", "https://api.external-llm.example.com/v1")
    monkeypatch.setenv("AIOPS_MODEL_API_KEY", "sk-secret-key-abcd")
    monkeypatch.setenv("AIOPS_MODEL_NAME", "gpt-5.4")

    caplog.set_level(logging.WARNING, logger="hermes.diagnosis_provider")
    config = dp.load_from_env()

    rec = next(r for r in caplog.records if "outgress" in r.getMessage())
    msg = rec.getMessage()
    assert "external-llm.example.com" in msg
    assert "gpt-5.4" in msg
    assert "abcd" in msg            # api_key 尾 4 位
    assert "出境" in msg or "egress" in msg.lower()
    assert "sk-secret-key-1234" not in caplog.text   # 全文不可出现:尾部是 abcd,不暴露前缀
    assert config.base_url == "https://api.external-llm.example.com/v1"


def test_load_from_env_outgress_log_internal(monkeypatch, caplog):
    monkeypatch.setenv("AIOPS_MODEL_BASE_URL", "http://model-service.default.svc.cluster.local/v1")
    monkeypatch.setenv("AIOPS_MODEL_API_KEY", "sk-internal-0000")
    monkeypatch.setenv("AIOPS_MODEL_NAME", "gpt-5.4")

    caplog.set_level(logging.WARNING, logger="hermes.diagnosis_provider")
    dp.load_from_env()

    rec = next(r for r in caplog.records if "outgress" in r.getMessage())
    msg = rec.getMessage()
    assert "model-service" in msg
    assert "gpt-5.4" in msg
    assert "internal" in msg
    assert "0000" in msg
    assert "sk-internal-0000" not in caplog.text


def test_load_from_env_requires_base_url_and_model(monkeypatch):
    monkeypatch.delenv("AIOPS_MODEL_BASE_URL", raising=False)
    monkeypatch.setenv("AIOPS_MODEL_NAME", "")
    with pytest.raises(dp.ProviderUnavailable) as exc_info:
        dp.load_from_env()
    assert exc_info.value.code == dp.PROVIDER_BAD_RESPONSE


def test_provider_timeout_reads_existing_env(monkeypatch):
    # 复用 AIOPS_HERMES_TOOL_TIMEOUT_SECONDS,缺省 3
    monkeypatch.delenv("AIOPS_HERMES_TOOL_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setenv("AIOPS_MODEL_BASE_URL", "http://x.internal.local/v1")
    monkeypatch.setenv("AIOPS_MODEL_API_KEY", "k")
    monkeypatch.setenv("AIOPS_MODEL_NAME", "m")
    config = dp.load_from_env()
    assert config.timeout_s == 3.0

    monkeypatch.setenv("AIOPS_HERMES_TOOL_TIMEOUT_SECONDS", "7")
    assert dp.load_from_env().timeout_s == 7.0


# --- import hygiene ----------------------------------------------------------

def test_module_imports_without_service_main(monkeypatch):
    # provider 模块不 import service_main(避免循环 + 保持边界);reload 须自洽。
    import sys

    monkeypatch.setitem(sys.modules, "hermes.service_main", None)
    importlib.reload(dp)