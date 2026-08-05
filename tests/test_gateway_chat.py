"""Gateway-owned Chat Session public contracts."""

from __future__ import annotations

from pathlib import Path
import json

import pytest

from apps.aiops_k8s_gateway.chat_sessions import ChatError, ChatSessions


class Clock:
    now = 1_000.0

    def __call__(self) -> float:
        return self.now


def _knowledge(answer: str) -> dict[str, object]:
    return {
        "mode": "knowledge", "answer": answer, "scope": None, "tool_activity": [],
        "evidence_references": [], "uncertainty": None, "next_step": None,
        "completion": {"status": "completed", "stopping_reason": "knowledge_answered"},
    }


def test_private_knowledge_chat_persists_and_expires_after_thirty_days(tmp_path: Path) -> None:
    clock = Clock()
    chats = ChatSessions(tmp_path / "gateway.db", clock=clock)
    session = chats.create("user-1", idempotency_key="create-1")
    calls: list[dict[str, object]] = []

    def respond(request: dict[str, object]) -> dict[str, object]:
        calls.append(request)
        visible = ChatSessions(tmp_path / "gateway.db", clock=clock).get("user-1", str(session["id"]))
        assert visible["messages"][-1]["status"] == "sending"
        return _knowledge("Deployment 管理一组可替换的 Pod。")

    session = chats.send(
        "user-1",
        str(session["id"]),
        content="什么是 Kubernetes Deployment？",
        idempotency_key="message-1",
        respond=respond,
    )

    assert [(item["role"], item["status"]) for item in session["messages"]] == [
        ("user", "completed"),
        ("assistant", "completed"),
    ]
    assert session["messages"][1]["content"] == "Deployment 管理一组可替换的 Pod。"
    assert calls[0]["messages"] == [{"role": "user", "content": "什么是 Kubernetes Deployment？"}]
    assert calls[0]["request_id"] == session["messages"][1]["id"]
    assert [event["type"] for event in chats.list_events("user-1", str(session["id"]))["events"]] == [
        "session.created", "message.created", "message.created", "message.completed",
    ]
    assert ChatSessions(tmp_path / "gateway.db", clock=clock).get("user-1", str(session["id"])) == session
    with pytest.raises(ChatError, match="not found") as hidden:
        chats.get("user-2", str(session["id"]))
    assert hidden.value.code == "chat_not_found"

    clock.now += 30 * 24 * 60 * 60 + 1
    assert chats.list("user-1") == []
    with pytest.raises(ChatError, match="not found"):
        chats.get("user-1", str(session["id"]))


def test_failed_message_is_idempotent_and_can_retry_after_reopen(tmp_path: Path) -> None:
    chats = ChatSessions(tmp_path / "gateway.db")
    session_id = str(chats.create("user-1", idempotency_key="create-1")["id"])
    calls = 0

    def unavailable(_request: dict[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise TimeoutError("private provider detail")

    failed = chats.send(
        "user-1", session_id, content="解释 PodDisruptionBudget", idempotency_key="message-1", respond=unavailable,
    )
    assistant = failed["messages"][-1]
    assert assistant["status"] == "failed"
    assert assistant["content"] == "暂时无法回答，请重试。"
    assert calls == 1
    assert chats.send(
        "user-1", session_id, content="解释 PodDisruptionBudget", idempotency_key="message-1", respond=unavailable,
    ) == failed
    assert calls == 1
    with pytest.raises(ChatError) as conflict:
        chats.send("user-1", session_id, content="不同问题", idempotency_key="message-1", respond=unavailable)
    assert conflict.value.code == "idempotency_conflict"

    recovered = ChatSessions(tmp_path / "gateway.db").retry(
        "user-1",
        session_id,
        str(assistant["id"]),
        respond=lambda _request: _knowledge("PDB 限制自愿中断时同时不可用的 Pod 数量。"),
    )
    assert recovered["messages"][-1]["status"] == "completed"
    assert recovered["messages"][-1]["content"].startswith("PDB 限制")


def test_chat_storage_and_model_context_exclude_credentials_and_secure_input(tmp_path: Path) -> None:
    chats = ChatSessions(tmp_path / "gateway.db")
    session_id = str(chats.create("user-1", idempotency_key="create-1")["id"])

    def respond(request: dict[str, object]) -> dict[str, object]:
        assert "user-secret" not in json.dumps(request)
        assert "secure-input" not in json.dumps(request)
        return _knowledge("authorization: Bearer model-secret")

    session = chats.send(
        "user-1",
        session_id,
        content="credential=user-secret {{secure-input:change-key}}",
        idempotency_key="message-1",
        respond=respond,
    )

    serialized = json.dumps(session)
    assert "user-secret" not in serialized
    assert "model-secret" not in serialized
    assert "secure-input" not in serialized
    assert "[REDACTED]" in serialized


def test_environment_scope_and_structured_result_survive_reopen_and_retry(tmp_path: Path) -> None:
    chats = ChatSessions(tmp_path / "gateway.db")
    session_id = str(chats.create("user-1", idempotency_key="create-1")["id"])
    scope = {
        "selection": {"deployment_target_id": "target-1"},
        "resources": [{
            "deployment_target_id": "target-1", "cluster_id": "cluster-1", "namespace": "shop",
            "service_id": "service-1", "service_name": "checkout", "workload_kind": "Deployment",
            "workload_name": "checkout-api",
        }],
        "time_range": {"type": "relative", "value": "30m"},
        "revision": "a" * 64,
    }

    def respond(request: dict[str, object]) -> dict[str, object]:
        assert request["scope"] == scope
        return {
            "mode": "environment", "answer": "错误率为 42%。", "scope": scope,
            "tool_activity": [{
                "tool": "query_metrics", "status": "succeeded", "summary": "error_rate=0.42",
                "authorized_scope": {"deployment_target_id": "target-1"},
                "raw_payload": {"token": "do-not-store"},
                "system_prompt": "do-not-store",
            }],
            "evidence_references": ["evidence:metrics:1"],
            "uncertainty": {"status": "accepted", "reasons": []},
            "next_step": "继续观察。",
            "completion": {"status": "accepted", "stopping_reason": "validated"},
        }

    session = chats.send(
        "user-1", session_id, content="现在错误率高吗？", idempotency_key="message-1",
        scope=scope, respond=respond,
    )
    assistant = session["messages"][-1]
    assert session["selected_scope"] == scope
    assert assistant["mode"] == "environment"
    assert assistant["evidence_references"] == ["evidence:metrics:1"]
    assert assistant["tool_activity"][0]["status"] == "succeeded"
    assert set(assistant["tool_activity"][0]) == {
        "tool", "status", "summary", "authorized_scope", "skill_versions",
    }
    assert assistant["tool_activity"][0]["skill_versions"] == []
    assert "do-not-store" not in json.dumps(session, ensure_ascii=False)
    assert ChatSessions(tmp_path / "gateway.db").get("user-1", session_id) == session


def test_environment_retry_refreezes_current_authorized_scope(tmp_path: Path) -> None:
    chats = ChatSessions(tmp_path / "gateway.db")
    session_id = str(chats.create("user-1", idempotency_key="create-1")["id"])
    scope = {
        "selection": {"deployment_target_id": "target-1"},
        "resources": [{
            "deployment_target_id": "target-1", "cluster_id": "cluster-1", "namespace": "shop",
            "service_id": "service-1", "service_name": "checkout", "workload_kind": "Deployment",
            "workload_name": "checkout-api",
        }],
        "time_range": {"type": "relative", "value": "30m"},
        "revision": "a" * 64,
    }
    failed = chats.send(
        "user-1", session_id, content="现在错误率高吗？", idempotency_key="message-1",
        scope=scope, respond=lambda _request: (_ for _ in ()).throw(TimeoutError()),
    )
    assistant_id = str(failed["messages"][-1]["id"])
    refreshed = {**scope, "revision": "b" * 64}
    expected_scope = refreshed

    with pytest.raises(ChatError) as denied:
        chats.retry("user-1", session_id, assistant_id, respond=lambda _request: {})
    assert denied.value.code == "chat_scope_not_found"

    def respond(request: dict[str, object]) -> dict[str, object]:
        assert request["scope"] == expected_scope
        return {
            "mode": "environment", "answer": "错误率为 42%。", "scope": expected_scope,
            "tool_activity": [{
                "tool": "query_metrics", "status": "succeeded", "summary": "error_rate=0.42",
                "authorized_scope": {"deployment_target_id": "target-1"},
            }],
            "evidence_references": ["evidence:metrics:1"],
            "uncertainty": {"status": "accepted", "reasons": []},
            "next_step": "继续观察。",
            "completion": {"status": "accepted", "stopping_reason": "validated"},
        }

    with pytest.raises(ChatError) as changed:
        chats.retry(
            "user-1", session_id, assistant_id, respond=respond,
            refreeze_scope=lambda selection: refreshed if selection == scope["selection"] else {},
        )
    assert changed.value.code == "chat_scope_changed"

    expected_scope = scope
    recovered = chats.retry(
        "user-1", session_id, assistant_id, respond=respond,
        refreeze_scope=lambda _selection: scope,
    )
    assert recovered["selected_scope"] == scope
    assert recovered["messages"][-1]["scope"] == scope
