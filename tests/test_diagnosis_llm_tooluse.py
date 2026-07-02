"""ADR-0003 child 2: thin LLM tool-use rewrite of run_diagnosis_session.

Strategy B (pure module, no HTTP server): inject a ScriptedProvider (child 1) and
fake adapters; conftest drives async tests with asyncio.run. Verifies the
LLM tool-use loop drives evidence collection + final diagnosis, falls back to the
keyword plan on provider failure, and applies the confidence guardrail.
"""

from __future__ import annotations

from typing import Any

import pytest

from hermes.diagnosis_provider import ProviderUnavailable, ScriptedProvider
from toolsets.incident_diagnosis import _build_tool_args_from_llm, _diagnosis_from_llm, run_diagnosis_session


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
        + '","action_type":"read"}],"confidence":{"score":'
        + str(score)
        + ',"level":"high"}}'
    )
    return {
        "choices": [
            {"finish_reason": "stop", "message": {"role": "assistant", "content": final_content}},
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }


def _tool_call_response(name: str = "query_metrics") -> dict:
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "call-1", "type": "function", "function": {"name": name, "arguments": "{}"}}
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


async def test_llm_tooluse_falls_back_to_keyword_plan_on_provider_failure() -> None:
    store = RecordingStore()

    class _BoomProvider:
        async def chat_with_tools(self, messages, tools):
            raise ProviderUnavailable("provider_unavailable", "endpoint down")

    session = await run_diagnosis_session(
        _incident("llm-inc-2"),
        metrics_adapter=_succeeded_adapter({"series": [1]}),
        logs_adapter=_succeeded_adapter({"lines": ["x"]}),
        topology_adapter=None,
        k8s_read_adapter=None,
        provider=_BoomProvider(),
        incident_store=store,
    )

    # fallback path: keyword diagnosis (build_diagnosis), keyword collector version
    assert session["collector_version"] == "incident_diagnosis/keyword-v1"
    assert "diagnosis" in session
    assert session["status"] in {"needs_human", "partial", "diagnosed"}
    # only true provider-visible tool calls land trace; fallback path records none
    assert store.traces == []


async def test_llm_tooluse_guardrail_caps_low_confidence_and_marks_degraded() -> None:
    """Model claims confidence 0.1 with full evidence → guardrails max with the floor, marks degraded."""
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
    # floor (_score_confidence) beats model's 0.1 → padded
    assert confidence["score"] > 0.1
    assert session["diagnosis"].get("degraded") is True


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


async def test_llm_tooluse_bad_final_json_falls_back() -> None:
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
    # bad final JSON → _diagnosis_from_llm raises → caller catches → keyword fallback:
    # diagnosis came from build_diagnosis, session marked the keyword collector version.
    # (The tool call before the bad answer did land a trace row — that is correct: trace
    # records what the model actually did, not the final outcome.)
    assert session["collector_version"] == "incident_diagnosis/keyword-v1"
    assert "diagnosis" in session


def test_llm_final_json_parser_accepts_fences_and_preface() -> None:
    parsed = _diagnosis_from_llm(
        "Here is the final diagnosis:\n"
        "```json\n"
        '{"root_cause_candidates":[{"cause":"bad deploy","category":"bad_release_deploy"}],'
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


async def test_llm_tooluse_max_turns_reads_runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """dev-external sets AIOPS_AGENT_MAX_TURNS; the live tool-use loop must honor it."""
    store = RecordingStore()
    provider = ScriptedProvider(
        [_tool_call_response("query_metrics") for _ in range(6)]
        + [_final_json_response("bad release caused repeated restarts")]
    )
    monkeypatch.setenv("AIOPS_AGENT_MAX_TURNS", "7")

    session = await run_diagnosis_session(
        _incident("llm-inc-max-turns"),
        metrics_adapter=_succeeded_adapter({"series": [1]}),
        logs_adapter=None,
        topology_adapter=None,
        k8s_read_adapter=None,
        provider=provider,
        incident_store=store,
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
