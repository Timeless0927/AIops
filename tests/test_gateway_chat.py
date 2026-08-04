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


def test_private_knowledge_chat_persists_and_expires_after_thirty_days(tmp_path: Path) -> None:
    clock = Clock()
    chats = ChatSessions(tmp_path / "gateway.db", clock=clock)
    session = chats.create("user-1", idempotency_key="create-1")
    calls: list[list[dict[str, str]]] = []

    def respond(messages: list[dict[str, str]]) -> str:
        calls.append(messages)
        visible = ChatSessions(tmp_path / "gateway.db", clock=clock).get("user-1", str(session["id"]))
        assert visible["messages"][-1]["status"] == "sending"
        return "Deployment 管理一组可替换的 Pod。"

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
    assert calls == [[{"role": "user", "content": "什么是 Kubernetes Deployment？"}]]
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

    def unavailable(_messages: list[dict[str, str]]) -> str:
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
        respond=lambda _messages: "PDB 限制自愿中断时同时不可用的 Pod 数量。",
    )
    assert recovered["messages"][-1]["status"] == "completed"
    assert recovered["messages"][-1]["content"].startswith("PDB 限制")


def test_chat_storage_and_model_context_exclude_credentials_and_secure_input(tmp_path: Path) -> None:
    chats = ChatSessions(tmp_path / "gateway.db")
    session_id = str(chats.create("user-1", idempotency_key="create-1")["id"])

    def respond(messages: list[dict[str, str]]) -> str:
        assert "user-secret" not in json.dumps(messages)
        assert "secure-input" not in json.dumps(messages)
        return "authorization: Bearer model-secret"

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
