"""ADR-0003 child 2: thin LLM tool-use rewrite of run_diagnosis_session.

Strategy B (pure module, no HTTP server): inject a ScriptedProvider (child 1) and
fake adapters; conftest drives async tests with asyncio.run. Verifies the
LLM tool-use loop drives evidence collection + final diagnosis, fails explicitly
on provider transport errors, and safely bounds invalid final responses.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from diagnosis_service.diagnosis_provider import (
    ProviderResult,
    ProviderUnavailable,
    ScriptedProvider,
    ToolCall,
)
from toolsets.diagnosis_session import (
    _build_tool_args_from_llm,
    _build_tooluse_system_prompt,
    _diagnosis_from_llm,
    run_diagnosis_session,
)


class RecordingStore:
    """Records add_evidence / add_diagnosis_trace / record_incident_diagnosis / record_cost calls."""

    def __init__(self) -> None:
        self.evidence: list[dict[str, Any]] = []
        self.traces: list[dict[str, Any]] = []
        self.costs: list[dict[str, Any]] = []

    async def add_evidence(self, incident_id, source_type, source_ref, summary, **kw):
        self.evidence.append(
            {"incident_id": incident_id, "source_type": source_type, "source_ref": source_ref, "summary": summary, **kw}
        )
        return len(self.evidence)

    async def add_diagnosis_trace(self, **kw):
        self.traces.append(kw)
        return len(self.traces)

    async def record_incident_diagnosis(self, incident_id, diagnosis):
        return None

    async def record_cost(self, *, model, input_tokens, output_tokens, estimated_cost, session_id, latency_ms):
        self.costs.append(
            {
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "estimated_cost": estimated_cost,
                "session_id": session_id,
                "latency_ms": latency_ms,
            }
        )


def _succeeded_adapter(payload):
    async def adapter(args):
        return {
            "status": "succeeded",
            "summary": "evidence collected",
            "data": payload,
            "evidence_refs": [{"ref_id": "ev-ref"}],
        }

    return adapter


def _final_json_response(root_cause: str, *, score: float = 0.9, action: str = "Collect more evidence") -> dict:
    final_content = (
        '{"root_cause_candidates":[{"cause":"'
        + root_cause
        + '","confidence":'
        + str(score)
        + ',"evidence_refs":["ev-ref"]}],"recommended_actions":[{"summary":"'
        + action
        + '"}],"confidence":{"score":'
        + str(score)
        + ',"level":"high"}}'
    )
    return {
        "choices": [
            {"finish_reason": "stop", "message": {"role": "assistant", "content": final_content}},
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }


def _tool_call_response(name: str = "query_metrics", *, query: str | None = None, call_id: str = "call-1") -> dict:
    arguments = {"query": query} if query is not None else {}
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 50, "completion_tokens": 5},
    }


def _incident(incident_id: str = "llm-inc-1") -> dict:
    return {
        "incident_id": incident_id,
        "session_id": "sess-" + incident_id,
        "alert_name": "payment 5xx error rate",
        "service": "payment-api",
        "namespace": "prod",
    }


def _pod_crash_incident(**overrides: Any) -> dict[str, Any]:
    incident = {
        "incident_id": "pod-crash-1",
        "session_id": "sess-pod-crash-1",
        "alert_name": "PodCrashLooping",
        "summary": "pod restart count is increasing",
        "cluster": "dev-external",
        "namespace": "demo-apps",
        "service": "",
        "workload_kind": "Deployment",
        "workload_name": "demo-probe",
        "pod_name": "demo-probe-7d9f4c78df-x2abc",
    }
    incident.update(overrides)
    return incident


def test_tooluse_prompt_requires_chinese_user_visible_text() -> None:
    prompt = _build_tooluse_system_prompt(_incident(), [])

    assert "用户可见" in prompt
    assert "中文" in prompt
    assert "PromQL" in prompt
    assert "不要重复相同基线" in prompt
    assert 'ALERTS{alertname="payment 5xx error rate",namespace="prod",service="payment-api",alertstate="firing"}' in prompt


async def test_llm_tooluse_runs_full_loop_and_records_final_diagnosis() -> None:
    store = RecordingStore()
    provider = ScriptedProvider(
        [_tool_call_response("query_metrics"), _final_json_response("upstream dependency timeout regression")]
    )
    session = await run_diagnosis_session(
        _incident(),
        metrics_adapter=_succeeded_adapter({"series": [1, 2]}),
        logs_adapter=None,
        topology_adapter=None,
        k8s_read_adapter=None,
        provider=provider,
        incident_store=store,
    )

    # model called one tool, then produced the final answer
    candidates = session["diagnosis"]["root_cause_candidates"]
    assert any("upstream dependency timeout regression" in c["cause"] for c in candidates)
    # evidence was collected (tool-use path used _collect_evidence like the keyword path)
    assert any(e["source_type"] == "metrics" for e in store.evidence)
    # trace row landed
    assert len(store.traces) == 1
    assert store.traces[0]["tool_name"] == "query_metrics"
    assert store.traces[0]["input_tokens"] == 50
    # collector marked llm-tooluse version
    assert session["collector_version"] == "incident_diagnosis/llm-tooluse-v1"


async def test_llm_tooluse_exposes_canonical_evidence_step_ids_to_recommendations() -> None:
    store = RecordingStore()

    class CanonicalRecommendationProvider:
        calls = 0

        async def chat_with_tools(self, messages, _tools):
            self.calls += 1
            if self.calls == 1:
                return ProviderResult(
                    {"role": "assistant", "content": None, "tool_calls": []},
                    [ToolCall("call-1", "query_metrics", {"query": "fault_active"})],
                    "tool_calls",
                    {"prompt_tokens": 1, "completion_tokens": 1},
                )
            tool_result = json.loads(messages[-1]["content"])
            assert tool_result["evidence_step_id"] == "sess-llm-canonical:step:3"
            content = json.dumps(
                {
                    "root_cause_candidates": [
                        {
                            "cause": "latched process readiness fault",
                            "confidence": 0.9,
                            "evidence_refs": ["ev-ref"],
                        }
                    ],
                    "recommended_actions": [
                        {
                            "summary": "Perform one controlled Deployment restart",
                            "change_intent": "controlled_restart",
                            "evidence_step_ids": [tool_result["evidence_step_id"]],
                            "safeguards": ["Keep the change inside the Incident namespace"],
                        }
                    ],
                    "confidence": {"score": 0.9, "level": "high"},
                }
            )
            return ProviderResult(
                {"role": "assistant", "content": content},
                [],
                "stop",
                {"prompt_tokens": 1, "completion_tokens": 1},
            )

    session = await run_diagnosis_session(
        _incident("llm-canonical"),
        metrics_adapter=_succeeded_adapter({"series": [1]}),
        provider=CanonicalRecommendationProvider(),
        incident_store=store,
    )

    assert session["steps"][0]["id"] == "sess-llm-canonical:step:1"
    assert session["steps"][0]["tool"] == "query_metrics"
    assert session["diagnosis"]["recommended_actions"][0] == {
        "summary": "Perform one controlled Deployment restart",
        "change_intent": "controlled_restart",
        "evidence_step_ids": ["sess-llm-canonical:step:3"],
        "safeguards": ["Keep the change inside the Incident namespace"],
    }


async def test_llm_tooluse_bounds_repeated_evidence_calls_before_writeback() -> None:
    store = RecordingStore()

    class RepeatingProvider:
        calls = 0

        async def chat_with_tools(self, _messages, _tools):
            self.calls += 1
            if self.calls == 1:
                calls = [
                    ToolCall(f"call-{index}", "query_metrics", {"query": "up"})
                    for index in range(120)
                ]
                return ProviderResult(
                    {"role": "assistant", "content": None, "tool_calls": []},
                    calls,
                    "tool_calls",
                    {"prompt_tokens": 1, "completion_tokens": 1},
                )
            return await ScriptedProvider([_final_json_response("bounded evidence")]).chat_with_tools(
                [], [],
            )

    session = await run_diagnosis_session(
        _incident("llm-bounded"),
        metrics_adapter=_succeeded_adapter({"series": [1]}),
        provider=RepeatingProvider(),
        incident_store=store,
    )

    assert len(session["steps"]) == 3
    assert len(store.evidence) == 3


async def test_llm_tooluse_stays_within_gateway_total_evidence_budget() -> None:
    store = RecordingStore()

    class MixedRepeatingProvider:
        calls = 0

        async def chat_with_tools(self, _messages, tools):
            self.calls += 1
            if not tools:
                return await ScriptedProvider([_final_json_response("bounded evidence")]).chat_with_tools([], [])
            if self.calls <= 6:
                calls = []
                for index in range(2):
                    for tool in ("query_metrics", "query_logs", "run_k8s_read", "get_service_topology"):
                        if tool == "query_metrics":
                            args = {"query": f"metric-{self.calls}-{index}"}
                        elif tool == "query_logs":
                            args = {"query": f'{{app="log-{self.calls}-{index}"}}'}
                        elif tool == "run_k8s_read":
                            args = {"selector": f"app.kubernetes.io/name=svc-{self.calls}-{index}"}
                        else:
                            args = {"service": f"svc-{self.calls}-{index}"}
                        calls.append(ToolCall(f"call-{self.calls}-{tool}-{index}", tool, args))
                return ProviderResult(
                    {"role": "assistant", "content": None, "tool_calls": []},
                    calls,
                    "tool_calls",
                    {"prompt_tokens": 1, "completion_tokens": 1},
                )
            return await ScriptedProvider([_final_json_response("bounded evidence")]).chat_with_tools([], [])

    incident = _incident("llm-total-budget")
    incident["service"] = ""
    adapter = _succeeded_adapter({"items": [{"metadata": {"name": "payment-api"}}]})
    session = await run_diagnosis_session(
        incident,
        metrics_adapter=adapter,
        logs_adapter=adapter,
        k8s_read_adapter=adapter,
        topology_adapter=adapter,
        provider=MixedRepeatingProvider(),
        incident_store=store,
    )

    assert len(session["steps"]) == 24
    assert len(store.evidence) == 24


async def test_llm_tooluse_caps_a_single_burst_of_tool_calls() -> None:
    store = RecordingStore()

    class BurstProvider:
        async def chat_with_tools(self, _messages, tools):
            if not tools:
                return await ScriptedProvider([_final_json_response("bounded burst")]).chat_with_tools([], [])
            calls = []
            for index in range(30):
                for tool in ("query_metrics", "query_logs", "run_k8s_read", "get_service_topology"):
                    if tool in {"query_metrics", "query_logs"}:
                        args = {"query": f"burst-{tool}-{index}"}
                    elif tool == "run_k8s_read":
                        args = {"selector": f"app.kubernetes.io/name=burst-{index}"}
                    else:
                        args = {"service": f"burst-{index}"}
                    calls.append(ToolCall(f"burst-{tool}-{index}", tool, args))
            return ProviderResult(
                {"role": "assistant", "content": None, "tool_calls": []},
                calls,
                "tool_calls",
                {},
            )

    adapter = _succeeded_adapter({"items": [{"metadata": {"name": "payment-api"}}]})
    incident = _incident("llm-burst-budget")
    incident["service"] = ""
    session = await run_diagnosis_session(
        incident,
        metrics_adapter=adapter,
        logs_adapter=adapter,
        k8s_read_adapter=adapter,
        topology_adapter=adapter,
        provider=BurstProvider(),
        incident_store=store,
    )

    assert len(session["steps"]) == 10
    assert session["status"] == "partial"
    assert session["missing_evidence"][-1]["reason"] == "evidence budget exhausted"


async def test_llm_tooluse_provider_failure_does_not_fall_back_to_keyword_plan() -> None:
    store = RecordingStore()

    class _BoomProvider:
        async def chat_with_tools(self, messages, tools):
            raise ProviderUnavailable("provider_unavailable", "endpoint down")

    with pytest.raises(ProviderUnavailable) as exc_info:
        await run_diagnosis_session(
            _incident("llm-inc-2"),
            metrics_adapter=_succeeded_adapter({"series": [1]}),
            logs_adapter=_succeeded_adapter({"lines": ["x"]}),
            topology_adapter=None,
            k8s_read_adapter=None,
            provider=_BoomProvider(),
            incident_store=store,
        )

    assert exc_info.value.code == "provider_unavailable"
    assert store.traces == []


async def test_late_provider_failure_exposes_already_collected_evidence() -> None:
    store = RecordingStore()

    class _LateFailureProvider:
        calls = 0

        async def chat_with_tools(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return await ScriptedProvider([_tool_call_response("query_metrics")]).chat_with_tools(
                    messages,
                    tools,
                )
            raise ProviderUnavailable("provider_unavailable", "endpoint down after evidence collection")

    with pytest.raises(ProviderUnavailable) as exc_info:
        await run_diagnosis_session(
            _incident("llm-inc-late-failure"),
            metrics_adapter=_succeeded_adapter({"series": [1]}),
            provider=_LateFailureProvider(),
            incident_store=store,
        )

    partial = exc_info.value.partial_result
    assert partial["status"] == "failed"
    assert partial["state_transitions"] == ["running", "failed"]
    assert len(partial["steps"]) == 3
    assert [step["evidence_ref"] for step in partial["steps"]] == ["ev-ref", None, "ev-ref"]
    assert len(store.evidence) == 3


async def test_llm_tooluse_rejects_confidence_without_evidence() -> None:
    store = RecordingStore()
    provider = ScriptedProvider([_final_json_response("weak guess", score=0.1)])
    session = await run_diagnosis_session(
        _incident("llm-inc-3"),
        metrics_adapter=None,
        logs_adapter=None,
        topology_adapter=None,
        k8s_read_adapter=None,
        provider=provider,
        incident_store=store,
    )

    confidence = session["diagnosis"]["confidence"]
    assert confidence == {"score": 0.0, "level": "low"}
    assert session["diagnosis"].get("degraded") is True
    assert session["completion_validation"]["status"] == "safe_partial"
    assert session["status"] == "needs_human"


async def test_llm_tooluse_no_provider_runs_keyword_path_unchanged() -> None:
    """provider=None keeps the legacy keyword behavior exactly (incl. collector version)."""
    store = RecordingStore()
    session = await run_diagnosis_session(
        _incident("llm-inc-4"),
        metrics_adapter=_succeeded_adapter({"series": [1]}),
        logs_adapter=None,
        topology_adapter=None,
        k8s_read_adapter=None,
        provider=None,
        incident_store=store,
    )
    assert session["collector_version"] == "incident_diagnosis/keyword-v1"
    assert session["status"] in {"needs_human", "partial", "diagnosed"}


async def test_llm_tooluse_bad_final_json_returns_safe_partial_without_keyword_fallback() -> None:
    store = RecordingStore()
    provider = ScriptedProvider(
        [_tool_call_response("query_metrics"), {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "not json at all"}}]}]
    )
    session = await run_diagnosis_session(
        _incident("llm-inc-5"),
        metrics_adapter=_succeeded_adapter({"series": [1]}),
        logs_adapter=None,
        topology_adapter=None,
        k8s_read_adapter=None,
        provider=provider,
        incident_store=store,
    )
    assert session["status"] == "partial"
    assert session["completion_validation"]["status"] == "safe_partial"
    assert session["completion_validation"]["repair_attempts"] == 1
    assert len(store.traces) == 1


async def test_llm_tooluse_invalid_structured_fields_return_safe_needs_human() -> None:
    provider = ScriptedProvider(
        [
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": '{"root_cause_candidates":[],"recommended_actions":[],"confidence":"high"}',
                        },
                    }
                ]
            }
        ]
    )

    session = await run_diagnosis_session(_incident("llm-invalid-fields"), provider=provider)

    assert session["status"] == "needs_human"
    assert session["state_transitions"] == ["running", "needs_human"]
    assert session["completion_validation"]["status"] == "safe_partial"
    assert session["diagnosis"]["confidence"] == {"score": 0.0, "level": "low"}


def test_llm_final_json_parser_accepts_fences_and_preface() -> None:
    parsed = _diagnosis_from_llm(
        "Here is the final diagnosis:\n"
        "```json\n"
        '{"root_cause_candidates":[{"cause":"bad deploy","category":"bad_release_deploy",'
        '"confidence":0.8,"evidence_refs":[]}],"recommended_actions":[],'
        '"confidence":{"score":0.8,"level":"high"}}\n'
        "```\n"
        "No mutation executed."
    )

    assert parsed["root_cause_candidates"][0]["category"] == "bad_release_deploy"
    assert parsed["confidence"]["score"] == 0.8


@pytest.mark.parametrize(
    "argv",
    [
        ["kubectl", "delete", "pod", "demo-probe-1", "-n", "demo-apps"],
        ["kubectl", "get", "pods", "-n", "default"],
        ["kubectl", "get", "pods", "--all-namespaces"],
        ["bash", "-lc", "kubectl get pods -n demo-apps"],
    ],
)
def test_llm_k8s_read_args_fall_back_to_safe_podcrash_defaults(argv: list[str]) -> None:
    args = _build_tool_args_from_llm(
        "run_k8s_read",
        _pod_crash_incident(),
        {"argv": argv, "command": "unsafe llm command"},
        [],
    )

    assert args["namespace"] == "demo-apps"
    assert args["selector"] == "app.kubernetes.io/name=demo-probe"
    assert args["argv"] == [
        "kubectl",
        "get",
        "pods",
        "-n",
        "demo-apps",
        "-l",
        "app.kubernetes.io/name=demo-probe",
    ]
    assert args["command"] == "kubectl get pods -n demo-apps -l app.kubernetes.io/name=demo-probe"


def test_llm_logs_args_clamp_cost_and_keep_required_scope() -> None:
    args = _build_tool_args_from_llm(
        "query_logs",
        _pod_crash_incident(),
        {
            "query": "{}",
            "time_range": {"type": "relative", "value": "24h"},
            "max_lines": 5000,
        },
        [],
    )

    assert args["cluster_id"] == "dev-external"
    assert args["namespace"] == "demo-apps"
    assert args["reason"]
    assert args["query"] == '{app="demo-probe"}'
    assert args["time_range"] == {"type": "relative", "value": "30m"}
    assert args["max_lines"] == 50


def test_llm_topology_args_prefer_workload_and_do_not_use_namespace_as_service() -> None:
    args = _build_tool_args_from_llm(
        "get_service_topology",
        _pod_crash_incident(),
        {"service": "demo-apps"},
        [],
    )
    missing_target_args = _build_tool_args_from_llm(
        "get_service_topology",
        _pod_crash_incident(workload_name="", pod_name=""),
        {"service": "demo-apps"},
        [],
    )

    assert args["service"] == "demo-probe"
    assert missing_target_args["service"] == ""


async def test_llm_tooluse_respects_injected_max_turns() -> None:
    store = RecordingStore()
    provider = ScriptedProvider(
        [_tool_call_response("query_metrics", query=f"metric-{index}", call_id=f"call-{index}") for index in range(6)]
        + [_final_json_response("bad release caused repeated restarts")]
    )
    session = await run_diagnosis_session(
        _incident("llm-inc-max-turns"),
        metrics_adapter=_succeeded_adapter({"series": [1]}),
        logs_adapter=None,
        topology_adapter=None,
        k8s_read_adapter=None,
        provider=provider,
        incident_store=store,
        max_turns=7,
    )

    assert session["collector_version"] == "incident_diagnosis/llm-tooluse-v1"
    assert any(
        "bad release" in c["cause"]
        for c in session["diagnosis"]["root_cause_candidates"]
    )
    assert len(store.traces) == 6


async def test_parent_ac_full_chain_smoke_four_channels_trace_and_cost_latency() -> None:
    """ADR-0003 parent AC #2: one fixture → ScriptedProvider → one session, four-channel
    evidence lands in store, diagnosis_trace ≥ 5 rows, cost_records.latency_ms > 0."""
    import time

    store = RecordingStore()

    class _LatentProvider:
        """ScriptedProvider wrapper that sleeps ~2ms per turn so the int(latency_ms) floor
        is non-zero — the bare loop runs sub-millisecond and int-truncates to 0."""

        def __init__(self) -> None:
            import asyncio as _asyncio

            self._inner = ScriptedProvider(
                [
                    _tool_call_response("query_metrics"),
                    _tool_call_response("query_logs"),
                    _tool_call_response("get_service_topology"),
                    _tool_call_response("run_k8s_read"),
                    _tool_call_response("query_metrics"),
                    _final_json_response("node memory pressure under eviction threshold"),
                ]
            )
            self._sleep = _asyncio.sleep

        async def chat_with_tools(self, messages, tools):
            await self._sleep(0.002)
            return await self._inner.chat_with_tools(messages, tools)

    provider = _LatentProvider()
    session = await run_diagnosis_session(
        _incident("llm-inc-ac2"),
        metrics_adapter=_succeeded_adapter({"series": [1, 2]}),
        logs_adapter=_succeeded_adapter({"lines": ["oom"]}),
        topology_adapter=_succeeded_adapter({"edges": []}),
        k8s_read_adapter=_succeeded_adapter({"pods": []}),
        provider=provider,
        incident_store=store,
        clock=time.monotonic,
    )

    # four-channel evidence landed
    channels = {e["source_type"] for e in store.evidence}
    assert {"metrics", "logs", "topology", "k8s_read"} <= channels, channels
    # diagnosis_trace ≥ 5 rows (one per tool call; the final answer contributes no trace row)
    assert len(store.traces) >= 5, len(store.traces)
    for row in store.traces:
        assert row["input_tokens"] == 50  # usage from _tool_call_response
    # cost_records.latency_ms > 0 — one cost row per provider turn (5 tool turns + 1 final = 6)
    assert store.costs, "no cost rows recorded"
    assert len(store.costs) == 6, len(store.costs)
    assert all(c["latency_ms"] >= 0 for c in store.costs)
    assert any(c["latency_ms"] > 0 for c in store.costs), [c["latency_ms"] for c in store.costs]
    # final diagnosis produced
    assert any("memory pressure" in c["cause"] for c in session["diagnosis"]["root_cause_candidates"])
