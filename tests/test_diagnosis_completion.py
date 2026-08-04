"""Public contracts for governed Diagnosis completion."""

from __future__ import annotations

import json
from typing import Any

import pytest

from diagnosis_service.diagnosis_provider import ProviderResult, ProviderUnavailable, ToolCall
from toolsets.diagnosis_session import run_diagnosis_session


def _incident() -> dict[str, str]:
    return {
        "incident_id": "completion-contract-1",
        "session_id": "session-completion-contract-1",
        "alert_name": "CheckoutErrorRateHigh",
        "summary": "checkout error rate is elevated",
        "cluster": "prod-a",
        "namespace": "payments",
        "service": "checkout-api",
    }


def _adapter(ref: str, data: dict[str, Any]):
    async def adapter(_args: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "succeeded",
            "summary": "evidence collected",
            "data": data,
            "evidence_refs": [{"ref_id": ref}],
        }

    return adapter


def _response(payload: dict[str, Any] | str) -> ProviderResult:
    return ProviderResult(
        {
            "role": "assistant",
            "content": payload if isinstance(payload, str) else json.dumps(payload),
        },
        [],
        "stop",
        {},
    )


async def test_observations_update_structured_hypothesis_relations() -> None:
    class Provider:
        async def chat_with_tools(self, _messages, _tools):
            return _response(
                {
                    "root_cause_candidates": [
                        {
                            "cause": "upstream dependency timeout",
                            "confidence": 0.8,
                            "evidence_refs": ["metrics-ref"],
                            "evidence_relations": [
                                {"evidence_ref": "metrics-ref", "relation": "supports"},
                                {"evidence_ref": "logs-ref", "relation": "refutes"},
                            ],
                            "unknowns": ["dependency saturation is not checked"],
                            "next_checks": ["inspect dependency saturation"],
                            "chain_of_thought": "must never persist",
                        }
                    ],
                    "recommended_actions": [],
                    "confidence": {"score": 0.8, "level": "high"},
                }
            )

    session = await run_diagnosis_session(
        _incident(),
        metrics_adapter=_adapter("metrics-ref", {"error_rate": 0.42}),
        logs_adapter=_adapter("logs-ref", {"timeout_count": 0}),
        provider=Provider(),
    )

    [candidate] = session["hypothesis_state"]["candidates"]
    assert candidate == {
        "cause": "upstream dependency timeout",
        "evidence_relations": [
            {"evidence_ref": "metrics-ref", "relation": "supports"},
            {"evidence_ref": "logs-ref", "relation": "refutes"},
        ],
        "unknowns": ["dependency saturation is not checked"],
        "next_checks": ["inspect dependency saturation"],
    }
    assert session["completion_validation"] == {
        "status": "accepted",
        "issues": [],
        "repair_attempts": 0,
        "stopping_reason": "validated",
        "remaining_evidence_steps": 22,
    }
    assert "must never persist" not in json.dumps(session)


async def test_invalid_final_json_is_repaired_once_with_validation_findings() -> None:
    class Provider:
        calls = 0
        repair_prompt = ""

        async def chat_with_tools(self, messages, _tools):
            self.calls += 1
            if self.calls == 1:
                return _response("not json: private draft")
            self.repair_prompt = messages[-1]["content"]
            return _response(
                {
                    "root_cause_candidates": [
                        {
                            "cause": "checkout upstream timeout",
                            "confidence": 0.6,
                            "evidence_refs": ["metrics-ref"],
                        }
                    ],
                    "recommended_actions": [],
                    "confidence": {"score": 0.6, "level": "medium"},
                }
            )

    provider = Provider()
    session = await run_diagnosis_session(
        _incident(),
        metrics_adapter=_adapter("metrics-ref", {"error_rate": 0.42}),
        logs_adapter=_adapter("logs-ref", {"timeout_count": 3}),
        provider=provider,
    )

    assert provider.calls == 2
    assert "assistant final content was not JSON" in provider.repair_prompt
    assert session["diagnosis"]["root_cause_candidates"][0]["cause"] == "checkout upstream timeout"
    assert session["completion_validation"]["repair_attempts"] == 1
    assert session["completion_validation"]["stopping_reason"] == "repaired"
    assert "private draft" not in json.dumps(session)


async def test_repeated_invalid_evidence_reference_returns_safe_partial() -> None:
    class Provider:
        calls = 0

        async def chat_with_tools(self, _messages, _tools):
            self.calls += 1
            return _response(
                {
                    "root_cause_candidates": [
                        {
                            "cause": "MODEL PRIVATE WRONG CONCLUSION",
                            "confidence": 0.99,
                            "evidence_refs": ["invented-ref"],
                        }
                    ],
                    "recommended_actions": [{"summary": "unsafe model advice"}],
                    "confidence": {"score": 0.99, "level": "high"},
                }
            )

    provider = Provider()
    session = await run_diagnosis_session(
        _incident(),
        metrics_adapter=_adapter("metrics-ref", {"error_rate": 0.42}),
        logs_adapter=_adapter("logs-ref", {"timeout_count": 3}),
        provider=provider,
    )

    assert provider.calls == 2
    assert session["status"] == "partial"
    assert session["completion_validation"]["status"] == "safe_partial"
    assert session["completion_validation"]["repair_attempts"] == 1
    assert session["completion_validation"]["stopping_reason"] == "repair_failed"
    assert "candidate references unknown Evidence" in session["completion_validation"]["issues"]
    assert session["diagnosis"]["confidence"] == {"score": 0.0, "level": "low"}
    assert session["diagnosis"]["recommended_actions"] == []
    serialized = json.dumps(session)
    assert "MODEL PRIVATE WRONG CONCLUSION" not in serialized
    assert "unsafe model advice" not in serialized
    assert "invented-ref" not in serialized


async def test_contradictory_tool_facts_cannot_both_support_a_candidate() -> None:
    async def metrics(args: dict[str, Any]) -> dict[str, Any]:
        baseline = str(args.get("query") or "").startswith("ALERTS{")
        return {
            "status": "succeeded",
            "summary": "metric checked",
            "data": {"error_rate": 0.42 if baseline else 0.0},
            "evidence_refs": [{"ref_id": "metrics-high" if baseline else "metrics-normal"}],
        }

    contradictory = {
        "root_cause_candidates": [
            {
                "cause": "checkout error rate is elevated",
                "confidence": 0.95,
                "evidence_refs": ["metrics-high", "metrics-normal"],
                "evidence_relations": [
                    {"evidence_ref": "metrics-high", "relation": "supports"},
                    {"evidence_ref": "metrics-normal", "relation": "supports"},
                ],
            }
        ],
        "recommended_actions": [],
        "confidence": {"score": 0.95, "level": "high"},
    }

    class Provider:
        calls = 0

        async def chat_with_tools(self, _messages, _tools):
            self.calls += 1
            if self.calls == 1:
                return ProviderResult(
                    {"role": "assistant", "content": None},
                    [ToolCall("metrics-current", "query_metrics", {"query": "current_error_rate"})],
                    "tool_calls",
                    {},
                )
            return _response(contradictory)

    provider = Provider()
    session = await run_diagnosis_session(
        _incident(),
        metrics_adapter=metrics,
        logs_adapter=_adapter("logs-ref", {"timeout_count": 3}),
        provider=provider,
    )

    assert provider.calls == 3
    assert session["status"] == "partial"
    assert "candidate supports contradictory tool facts" in session["completion_validation"]["issues"]
    assert session["completion_validation"]["stopping_reason"] == "repair_failed"
    assert session["diagnosis"]["confidence"]["score"] == 0.0


async def test_premature_stop_uses_remaining_budget_for_required_sources() -> None:
    valid = {
        "root_cause_candidates": [
            {
                "cause": "checkout dependency timeout",
                "confidence": 0.7,
                "evidence_refs": ["metrics-ref", "logs-ref", "topology-ref", "k8s-ref"],
            }
        ],
        "recommended_actions": [],
        "confidence": {"score": 0.7, "level": "medium"},
    }

    class Provider:
        calls = 0
        repair_prompt = ""

        async def chat_with_tools(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return _response(valid)
            if self.calls == 2:
                self.repair_prompt = messages[-1]["content"]
                assert tools
                return ProviderResult(
                    {"role": "assistant", "content": None},
                    [
                        ToolCall("topology-check", "get_service_topology", {}),
                        ToolCall("k8s-check", "run_k8s_read", {}),
                    ],
                    "tool_calls",
                    {},
                )
            return _response(valid)

    provider = Provider()
    session = await run_diagnosis_session(
        _incident(),
        metrics_adapter=_adapter("metrics-ref", {"error_rate": 0.42}),
        logs_adapter=_adapter("logs-ref", {"timeout_count": 3}),
        topology_adapter=_adapter("topology-ref", {"edge_count": 2}),
        k8s_read_adapter=_adapter("k8s-ref", {"ready_replicas": 3}),
        provider=provider,
    )

    assert provider.calls == 3
    assert "required source was not checked or explained: topology" in provider.repair_prompt
    assert "required source was not checked or explained: k8s_read" in provider.repair_prompt
    assert session["status"] == "diagnosed"
    assert session["completion_validation"]["status"] == "accepted"
    assert session["completion_validation"]["repair_attempts"] == 1
    assert session["completion_validation"]["remaining_evidence_steps"] == 20


async def test_unavailable_required_sources_have_concrete_missing_reasons() -> None:
    class Provider:
        calls = 0

        async def chat_with_tools(self, _messages, _tools):
            self.calls += 1
            return _response(
                {
                    "root_cause_candidates": [
                        {
                            "cause": "checkout dependency timeout",
                            "confidence": 0.6,
                            "evidence_refs": ["metrics-ref"],
                        }
                    ],
                    "recommended_actions": [],
                    "confidence": {"score": 0.6, "level": "medium"},
                }
            )

    provider = Provider()
    session = await run_diagnosis_session(
        _incident(),
        metrics_adapter=_adapter("metrics-ref", {"error_rate": 0.42}),
        logs_adapter=_adapter("logs-ref", {"timeout_count": 3}),
        provider=provider,
    )

    missing = {item["source_type"]: item["reason"] for item in session["missing_evidence"]}
    assert provider.calls == 1
    assert missing["topology"] == "required topology adapter is unavailable"
    assert missing["k8s_read"] == "required k8s_read adapter is unavailable"
    assert session["status"] == "partial"


async def test_recommended_action_must_reference_accepted_evidence_step() -> None:
    class Provider:
        calls = 0

        async def chat_with_tools(self, _messages, _tools):
            self.calls += 1
            return _response(
                {
                    "root_cause_candidates": [
                        {
                            "cause": "checkout dependency timeout",
                            "confidence": 0.6,
                            "evidence_refs": ["metrics-ref"],
                        }
                    ],
                    "recommended_actions": [
                        {
                            "summary": "inspect the dependency",
                            "evidence_step_ids": ["invented-step"],
                        }
                    ],
                    "confidence": {"score": 0.6, "level": "medium"},
                }
            )

    provider = Provider()
    session = await run_diagnosis_session(
        _incident(),
        metrics_adapter=_adapter("metrics-ref", {"error_rate": 0.42}),
        logs_adapter=_adapter("logs-ref", {"timeout_count": 3}),
        provider=provider,
    )

    assert provider.calls == 2
    assert "recommended action references unknown Evidence Step" in session["completion_validation"]["issues"]
    assert session["completion_validation"]["status"] == "safe_partial"
    assert session["diagnosis"]["recommended_actions"] == []
    assert "invented-step" not in json.dumps(session)


async def test_budget_stop_still_validates_and_bounds_final_repair() -> None:
    class Provider:
        calls = 0

        async def chat_with_tools(self, _messages, tools):
            self.calls += 1
            if tools:
                return ProviderResult(
                    {"role": "assistant", "content": None},
                    [
                        ToolCall(f"metric-{index}", "query_metrics", {"query": f"metric_{index}"})
                        for index in range(9)
                    ],
                    "tool_calls",
                    {},
                )
            return _response(
                {
                    "root_cause_candidates": [
                        {
                            "cause": "MODEL BUDGET CONCLUSION",
                            "confidence": 0.99,
                            "evidence_refs": ["invented-ref"],
                        }
                    ],
                    "recommended_actions": [],
                    "confidence": {"score": 0.99, "level": "high"},
                }
            )

    provider = Provider()
    session = await run_diagnosis_session(
        _incident(),
        metrics_adapter=_adapter("metrics-ref", {"error_rate": 0.42}),
        logs_adapter=_adapter("logs-ref", {"timeout_count": 3}),
        provider=provider,
    )

    assert provider.calls == 3
    assert session["completion_validation"]["status"] == "safe_partial"
    assert session["completion_validation"]["repair_attempts"] == 1
    assert session["status"] == "partial"
    assert "MODEL BUDGET CONCLUSION" not in json.dumps(session)


async def test_tool_turn_updates_safe_hypothesis_state_before_next_call() -> None:
    class Provider:
        calls = 0

        async def chat_with_tools(self, _messages, _tools):
            self.calls += 1
            if self.calls == 1:
                content = json.dumps(
                    {
                        "hypothesis_state": {
                            "candidates": [
                                {
                                    "cause": "checkout upstream timeout",
                                    "evidence_relations": [
                                        {"evidence_ref": "metrics-ref", "relation": "supports"},
                                        {"evidence_ref": "logs-ref", "relation": "refutes"},
                                    ],
                                    "unknowns": ["topology is not checked"],
                                    "next_checks": ["query service topology"],
                                    "chain_of_thought": "private turn reasoning",
                                }
                            ]
                        }
                    }
                )
                return ProviderResult(
                    {"role": "assistant", "content": content},
                    [ToolCall("topology-check", "get_service_topology", {})],
                    "tool_calls",
                    {},
                )
            raise ProviderUnavailable("provider_unavailable", "provider stopped")

    with pytest.raises(ProviderUnavailable) as exc_info:
        await run_diagnosis_session(
            _incident(),
            metrics_adapter=_adapter("metrics-ref", {"error_rate": 0.42}),
            logs_adapter=_adapter("logs-ref", {"timeout_count": 3}),
            topology_adapter=_adapter("topology-ref", {"edge_count": 2}),
            provider=Provider(),
        )

    [candidate] = exc_info.value.partial_result["hypothesis_state"]["candidates"]
    assert candidate["cause"] == "checkout upstream timeout"
    assert candidate["evidence_relations"] == [
        {"evidence_ref": "metrics-ref", "relation": "supports"},
        {"evidence_ref": "logs-ref", "relation": "refutes"},
        {"evidence_ref": "topology-ref", "relation": "uncertain"},
    ]
    assert candidate["unknowns"] == ["topology is not checked"]
    assert candidate["next_checks"] == ["query service topology"]
    assert "private turn reasoning" not in json.dumps(exc_info.value.partial_result)


async def test_model_tool_calls_cannot_widen_incident_resource_scope() -> None:
    captured: dict[str, dict[str, Any]] = {}

    def scoped_adapter(tool: str, ref: str):
        async def adapter(args: dict[str, Any]) -> dict[str, Any]:
            captured[tool] = args
            return {
                "status": "succeeded",
                "summary": f"{tool} checked",
                "data": {"checked": True},
                "evidence_refs": [{"ref_id": ref}],
            }

        return adapter

    class Provider:
        calls = 0

        async def chat_with_tools(self, _messages, _tools):
            self.calls += 1
            if self.calls == 1:
                return ProviderResult(
                    {"role": "assistant", "content": None},
                    [
                        ToolCall(
                            "wide-k8s",
                            "run_k8s_read",
                            {"argv": ["kubectl", "get", "pods", "--all-namespaces"]},
                        ),
                        ToolCall("wide-topology", "get_service_topology", {"service": "other-service"}),
                    ],
                    "tool_calls",
                    {},
                )
            return _response(
                {
                    "root_cause_candidates": [
                        {
                            "cause": "checkout dependency timeout",
                            "confidence": 0.7,
                            "evidence_refs": ["metrics-ref", "logs-ref", "topology-ref", "k8s-ref"],
                        }
                    ],
                    "recommended_actions": [],
                    "confidence": {"score": 0.7, "level": "medium"},
                }
            )

    session = await run_diagnosis_session(
        _incident(),
        metrics_adapter=scoped_adapter("metrics", "metrics-ref"),
        logs_adapter=scoped_adapter("logs", "logs-ref"),
        topology_adapter=scoped_adapter("topology", "topology-ref"),
        k8s_read_adapter=scoped_adapter("k8s", "k8s-ref"),
        provider=Provider(),
    )

    for args in captured.values():
        assert args["cluster_id"] == "prod-a"
        assert args["namespace"] == "payments"
        assert args["service"] == "checkout-api"
    assert captured["topology"]["service"] == "checkout-api"
    assert "--all-namespaces" not in captured["k8s"]["argv"]
    assert session["status"] == "diagnosed"
    assert session["completion_validation"]["status"] == "accepted"


async def test_stop_is_rejected_when_candidate_declares_an_available_next_check() -> None:
    final = {
        "root_cause_candidates": [
            {
                "cause": "checkout dependency timeout",
                "confidence": 0.7,
                "evidence_refs": ["metrics-ref", "logs-ref", "topology-ref", "k8s-ref"],
                "next_checks": ["query another bounded metric"],
            }
        ],
        "recommended_actions": [],
        "confidence": {"score": 0.7, "level": "medium"},
    }

    class Provider:
        calls = 0

        async def chat_with_tools(self, _messages, _tools):
            self.calls += 1
            return _response(final)

    provider = Provider()
    session = await run_diagnosis_session(
        _incident(),
        metrics_adapter=_adapter("metrics-ref", {"error_rate": 0.42}),
        logs_adapter=_adapter("logs-ref", {"timeout_count": 3}),
        topology_adapter=_adapter("topology-ref", {"edge_count": 2}),
        k8s_read_adapter=_adapter("k8s-ref", {"ready_replicas": 3}),
        provider=provider,
    )

    assert provider.calls == 2
    assert "candidate declares a remaining check while Evidence budget is available" in session["completion_validation"]["issues"]
    assert session["completion_validation"]["status"] == "safe_partial"
    assert session["status"] == "partial"


async def test_model_turn_budget_exhaustion_returns_safe_partial() -> None:
    class Provider:
        calls = 0

        async def chat_with_tools(self, _messages, _tools):
            self.calls += 1
            return ProviderResult(
                {"role": "assistant", "content": None},
                [
                    ToolCall(
                        f"metric-{self.calls}",
                        "query_metrics",
                        {"query": f"bounded_metric_{self.calls}"},
                    )
                ],
                "tool_calls",
                {},
            )

    provider = Provider()
    session = await run_diagnosis_session(
        _incident(),
        metrics_adapter=_adapter("metrics-ref", {"error_rate": 0.42}),
        logs_adapter=_adapter("logs-ref", {"timeout_count": 3}),
        provider=provider,
        max_turns=2,
    )

    assert provider.calls == 2
    assert session["status"] == "partial"
    assert session["completion_validation"]["status"] == "safe_partial"
    assert session["completion_validation"]["repair_attempts"] == 0
    assert session["completion_validation"]["stopping_reason"] == "model_turn_budget_exhausted"
