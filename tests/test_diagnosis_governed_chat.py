from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from aiops.contracts.governed_tools import default_capability_snapshot
from diagnosis_service.chat_loop_checkpoints import ChatLoopCheckpoints
from diagnosis_service.diagnosis_provider import ProviderResult, ToolCall
from diagnosis_service.governed_chat import GovernedChatError, answer_governed_chat


def _scope() -> dict[str, object]:
    return {
        "selection": {"cluster_id": "cluster-prod", "deployment_target_id": "target-checkout"},
        "resources": [
            {
                "deployment_target_id": "target-checkout",
                "cluster_id": "cluster-prod",
                "namespace": "shop",
                "service_id": "service-checkout",
                "service_name": "checkout",
                "workload_kind": "Deployment",
                "workload_name": "checkout-api",
            }
        ],
        "time_range": {"type": "relative", "value": "30m"},
        "revision": "a" * 64,
    }


def _request(request_id: str = "chat-run-1", *, scope: dict[str, object] | None = None) -> dict[str, object]:
    request: dict[str, object] = {
        "request_id": request_id,
        "messages": [{"role": "user", "content": "checkout 现在错误率高吗？"}],
    }
    if scope is not None:
        request.update({"scope": scope, "capabilities": default_capability_snapshot()})
    return request


def _tool(name: str = "query_metrics", arguments: dict[str, object] | None = None) -> ProviderResult:
    call = ToolCall("call-1", name, arguments or {"deployment_target_id": "target-checkout"})
    return ProviderResult(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }],
        },
        [call],
        "tool_calls",
        {},
    )


def _final(refs: list[str]) -> ProviderResult:
    return ProviderResult(
        {
            "role": "assistant",
            "content": json.dumps({
                "root_cause_candidates": [{
                    "cause": "checkout 当前错误率为 42%。",
                    "confidence": 0.8,
                    "evidence_refs": refs,
                }],
                "recommended_actions": [],
                "confidence": {"score": 0.8, "level": "high"},
            }, ensure_ascii=False),
        },
        [],
        "stop",
        {},
    )


class Provider:
    def __init__(self, *turns: ProviderResult) -> None:
        self.turns = list(turns)
        self.calls = 0

    async def chat_with_tools(self, _messages, tools):
        self.calls += 1
        assert tools
        return self.turns.pop(0)


async def _evidence(_args: dict[str, Any]) -> dict[str, object]:
    return {
        "status": "succeeded",
        "summary": "checkout error_rate=0.42",
        "data": {"error_rate": 0.42},
        "evidence_refs": [{"ref_id": "evidence:metrics:1"}],
        "audit": {"source": "prometheus"},
    }


def test_knowledge_profile_exposes_no_tools_and_uses_the_shared_checkpoint(tmp_path: Path) -> None:
    class KnowledgeProvider:
        async def chat_with_tools(self, messages, tools):
            assert tools == []
            assert messages[-1]["content"] == "什么是滚动发布？"
            return ProviderResult(
                {"role": "assistant", "content": "滚动发布会逐步替换旧实例。"}, [], "stop", {},
            )

    result = asyncio.run(answer_governed_chat(
        {"request_id": "knowledge-1", "messages": [{"role": "user", "content": "什么是滚动发布？"}]},
        provider=KnowledgeProvider(),
        checkpoints=ChatLoopCheckpoints(tmp_path / "diagnosis.db"),
        adapters={},
    ))
    assert result == {
        "mode": "knowledge",
        "answer": "滚动发布会逐步替换旧实例。",
        "scope": None,
        "tool_activity": [],
        "evidence_references": [],
        "uncertainty": None,
        "next_step": None,
        "completion": {"status": "completed", "stopping_reason": "knowledge_answered"},
    }


def test_environment_profile_requires_an_authorized_observation_and_citation(tmp_path: Path) -> None:
    provider = Provider(_tool(), _final(["evidence:metrics:1"]))
    result = asyncio.run(answer_governed_chat(
        _request(scope=_scope()),
        provider=provider,
        checkpoints=ChatLoopCheckpoints(tmp_path / "diagnosis.db"),
        adapters={"query_metrics": _evidence},
    ))
    assert result["mode"] == "environment"
    assert result["answer"] == "checkout 当前错误率为 42%。"
    assert result["evidence_references"] == ["evidence:metrics:1"]
    assert result["completion"]["status"] == "accepted"
    assert result["tool_activity"][0]["authorized_scope"]["deployment_target_id"] == "target-checkout"


def test_environment_scope_requires_the_frozen_selection_contract(tmp_path: Path) -> None:
    scope = _scope()
    del scope["selection"]

    with pytest.raises(GovernedChatError) as rejected:
        asyncio.run(answer_governed_chat(
            _request(scope=scope),
            provider=Provider(_tool()),
            checkpoints=ChatLoopCheckpoints(tmp_path / "diagnosis.db"),
            adapters={"query_metrics": _evidence},
        ))
    assert rejected.value.code == "invalid_scope"


@pytest.mark.parametrize(
    ("tool", "arguments", "change"),
    [
        ("query_metrics", {"deployment_target_id": "target-secret"}, None),
        ("delete_resource", {"deployment_target_id": "target-checkout"}, None),
        ("query_metrics", {"deployment_target_id": "target-checkout"}, {"enabled": False}),
        ("query_metrics", {"deployment_target_id": "target-checkout"}, {"version": "changed"}),
        ("query_metrics", {"deployment_target_id": "target-checkout"}, {"read_only": False}),
    ],
)
def test_scope_expansion_unknown_disabled_changed_and_mutation_like_tools_fail_closed(
    tmp_path: Path,
    tool: str,
    arguments: dict[str, object],
    change: dict[str, object] | None,
) -> None:
    request = _request(request_id=f"denied-{tool}-{change}", scope=_scope())
    if change:
        capabilities = request["capabilities"]
        assert isinstance(capabilities, dict)
        capabilities["query_metrics"] = {**capabilities["query_metrics"], **change}
    calls = 0

    async def must_not_run(_args):
        nonlocal calls
        calls += 1
        return await _evidence(_args)

    result = asyncio.run(answer_governed_chat(
        request,
        provider=Provider(_tool(tool, arguments), _final([]), _final([])),
        checkpoints=ChatLoopCheckpoints(tmp_path / "diagnosis.db"),
        adapters={"query_metrics": must_not_run},
    ))
    assert calls == 0
    assert result["answer"] == "无法取得授权的实时 Observation，不能确认当前环境状态。"
    assert result["evidence_references"] == []
    assert result["completion"]["status"] == "safe_partial"
    assert result["tool_activity"][0]["status"] == "skipped"


def test_budget_exhaustion_returns_a_cited_partial_result(tmp_path: Path) -> None:
    result = asyncio.run(answer_governed_chat(
        _request(scope=_scope()),
        provider=Provider(_tool()),
        checkpoints=ChatLoopCheckpoints(tmp_path / "diagnosis.db"),
        adapters={"query_metrics": _evidence},
        max_turns=1,
    ))
    assert result["completion"]["stopping_reason"] == "model_turn_budget_exhausted"
    assert result["completion"]["status"] == "safe_partial"
    assert result["evidence_references"] == ["evidence:metrics:1"]


def test_restart_reuses_completed_tool_observation(tmp_path: Path) -> None:
    db_path = tmp_path / "diagnosis.db"
    calls = 0

    async def evidence_then_stop(args):
        nonlocal calls
        calls += 1
        return await _evidence(args)

    class StopsAfterTool:
        turn = 0

        async def chat_with_tools(self, _messages, _tools):
            self.turn += 1
            if self.turn == 1:
                return _tool()
            raise SystemExit("Diagnosis stopped after the accepted tool checkpoint")

    with pytest.raises(SystemExit):
        asyncio.run(answer_governed_chat(
            _request(scope=_scope()),
            provider=StopsAfterTool(),
            checkpoints=ChatLoopCheckpoints(db_path),
            adapters={"query_metrics": evidence_then_stop},
        ))

    async def repeated(_args):
        pytest.fail("completed tool call repeated after restart")

    result = asyncio.run(answer_governed_chat(
        _request(scope=_scope()),
        provider=Provider(_final(["evidence:metrics:1"])),
        checkpoints=ChatLoopCheckpoints(db_path),
        adapters={"query_metrics": repeated},
    ))
    assert calls == 1
    assert result["evidence_references"] == ["evidence:metrics:1"]
