"""Gateway-owned Chat Session public contracts."""

from __future__ import annotations

from pathlib import Path
import json
import sqlite3
from threading import Event, Thread

import pytest

from apps.aiops_k8s_gateway.chat_attachments import ChatAttachments
from apps.aiops_k8s_gateway.chat_sessions import (
    _MANAGEMENT_SCHEMA,
    _RESULT_SCHEMA,
    _SCHEMA,
    ChatError,
    ChatSessions,
)


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


def test_private_knowledge_chat_persists_until_explicit_deletion(tmp_path: Path) -> None:
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
    assert chats.list("user-1")[0]["id"] == session["id"]
    assert chats.get("user-1", str(session["id"]))["expires_at"] is None


def test_streamed_answer_is_replayable_and_can_be_cancelled(tmp_path: Path) -> None:
    chats = ChatSessions(tmp_path / "gateway.db")
    session_id = str(chats.create("user-1", idempotency_key="create-1")["id"])
    answer = "Deployment 会逐步替换 Pod，并持续检查可用副本。"
    completed = chats.send(
        "user-1", session_id, content="解释 Deployment", idempotency_key="message-1",
        respond=lambda _: _knowledge(answer), stream=True,
    )
    events = chats.list_events("user-1", session_id)["events"]
    deltas = [event["payload"]["delta"] for event in events if event["type"] == "message.delta"]
    assert len(deltas) > 1
    assert "".join(deltas) == answer
    assert completed["messages"][-1]["content"] == answer
    assert events[-1]["type"] == "message.completed"

    started = Event()
    release = Event()

    def delayed(_request: dict[str, object]) -> dict[str, object]:
        started.set()
        release.wait(2)
        return _knowledge("这段内容不应在取消后写入。")

    result: list[dict[str, object]] = []
    worker = Thread(target=lambda: result.append(chats.send(
        "user-1", session_id, content="停止这个回答", idempotency_key="message-2",
        respond=delayed, stream=True,
    )))
    worker.start()
    assert started.wait(2)
    cancelled = chats.cancel("user-1", session_id, idempotency_key="cancel-1")
    assert chats.cancel("user-1", session_id, idempotency_key="cancel-1") == cancelled
    release.set()
    worker.join(2)
    assert not worker.is_alive()
    assert result[0]["messages"][-1]["completion"] == {
        "status": "cancelled", "stopping_reason": "user_cancelled",
    }
    assert result[0]["messages"][-1]["content"] == ""
    assert chats.list_events("user-1", session_id)["events"][-1]["type"] == "message.cancelled"


def test_branch_migration_preserves_existing_message_identity_and_order(tmp_path: Path) -> None:
    path = tmp_path / "gateway.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)")
        conn.executescript(_SCHEMA)
        conn.executescript(_RESULT_SCHEMA)
        conn.executescript(_MANAGEMENT_SCHEMA)
        conn.executemany("INSERT INTO schema_migrations VALUES (?, 1)", [(45,), (46,), (51,)])
        conn.execute(
            """INSERT INTO chat_sessions
               (id, owner_id, title, create_key, create_hash, created_at, updated_at, expires_at)
               VALUES ('session-1', 'user-1', '旧会话', 'create-1', ?, 1, 2, 3)""",
            ("a" * 64,),
        )
        conn.execute(
            """INSERT INTO chat_messages
               (id, session_id, role, status, position, content, created_at, updated_at)
               VALUES ('user-1', 'session-1', 'user', 'completed', 1, '旧问题', 1, 1)""",
        )
        conn.execute(
            """INSERT INTO chat_messages
               (id, session_id, role, status, position, content, reply_to_id, created_at, updated_at)
               VALUES ('assistant-1', 'session-1', 'assistant', 'completed', 2, '旧回答', 'user-1', 2, 2)""",
        )

    migrated = ChatSessions(path).get("user-1", "session-1")
    assert [(message["id"], message["parent_id"]) for message in migrated["messages"]] == [
        ("user-1", None), ("assistant-1", "user-1"),
    ]
    assert migrated["current_branch_head_id"] == "assistant-1"


def test_session_management_search_sort_archive_restore_and_idempotent_delete(tmp_path: Path) -> None:
    clock = Clock()
    chats = ChatSessions(tmp_path / "gateway.db", clock=clock)
    first = chats.create("user-1", idempotency_key="create-1")
    second = chats.create("user-1", idempotency_key="create-2")
    chats.update("user-1", str(first["id"]), idempotency_key="rename-1", title="重要排障")
    renamed = chats.send("user-1", str(first["id"]), content="不会覆盖标题", idempotency_key="message-title", respond=lambda _: _knowledge("已保留"))
    assert renamed["title"] == "重要排障"
    chats.send("user-1", str(second["id"]), content="查询 checkout 错误率", idempotency_key="message-1", respond=lambda _: _knowledge("42%"))
    pinned = chats.update("user-1", str(second["id"]), idempotency_key="pin-1", pinned=True)
    assert pinned["pinned"] is True
    assert [item["id"] for item in chats.list("user-1")] == [second["id"], first["id"]]
    assert [item["id"] for item in chats.list("user-1", filter="pinned")] == [second["id"]]
    assert [item["id"] for item in chats.list("user-1", query="checkout")] == [second["id"]]

    archived = chats.update("user-1", str(second["id"]), idempotency_key="archive-1", archived=True)
    assert archived["archived"] is True and archived["pinned"] is False
    normal = chats.list("user-1")
    assert len(normal) == 1
    assert normal[0]["id"] == first["id"]
    assert normal[0]["title"] == "重要排障"
    assert normal[0]["title_manual"] is True
    assert normal[0]["pinned"] is False and normal[0]["archived"] is False
    assert [item["id"] for item in chats.list("user-1", filter="archived")] == [second["id"]]
    assert [item["id"] for item in chats.list("user-1", query="checkout")] == [second["id"]]

    deleted = chats.delete("user-1", str(first["id"]), idempotency_key="delete-1")
    assert deleted == chats.delete("user-1", str(first["id"]), idempotency_key="delete-1")
    with pytest.raises(ChatError) as hidden:
        chats.get("user-2", str(second["id"]))
    assert hidden.value.code == "chat_not_found"


def test_delete_succeeds_when_post_commit_attachment_cleanup_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    chats = ChatSessions(tmp_path / "gateway.db")
    session_id = str(chats.create("user-1", idempotency_key="create-1")["id"])

    def fail_cleanup(_attachments: ChatAttachments) -> None:
        raise OSError("blob storage unavailable")

    monkeypatch.setattr(ChatAttachments, "collect_garbage", fail_cleanup)

    deleted = chats.delete("user-1", session_id, idempotency_key="delete-1")

    assert deleted == {"chat_session_id": session_id, "deleted": True}
    with pytest.raises(ChatError) as missing:
        chats.get("user-1", session_id)
    assert missing.value.code == "chat_not_found"


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


def test_edit_and_reload_create_immutable_sibling_branches_and_scoped_context(tmp_path: Path) -> None:
    chats = ChatSessions(tmp_path / "gateway.db")
    session_id = str(chats.create("user-1", idempotency_key="create-1")["id"])
    contexts: list[list[dict[str, str]]] = []
    first = chats.send(
        "user-1", session_id, content="原问题", idempotency_key="message-1",
        respond=lambda request: (_knowledge(contexts.append(request["messages"]) or "原回答")),
    )
    original_user, original_assistant = first["messages"]

    edited = chats.edit(
        "user-1", session_id, str(original_user["id"]), content="修改后的问题", idempotency_key="edit-1",
        respond=lambda request: (_knowledge(contexts.append(request["messages"]) or "修改后的回答")),
    )
    edited_user, edited_assistant = edited["messages"][-2:]
    assert edited_user["id"] != original_user["id"]
    assert edited_assistant["id"] != original_assistant["id"]
    assert edited_user["parent_id"] is None
    assert edited_assistant["parent_id"] == edited_user["id"]
    assert original_user["content"] == "原问题"
    assert original_assistant["content"] == "原回答"
    assert contexts[-1] == [{"role": "user", "content": "修改后的问题"}]
    assert edited["current_branch_head_id"] == edited_assistant["id"]
    assert [item["id"] for item in edited["messages"] if item["is_current_branch"]] == [edited_user["id"], edited_assistant["id"]]

    reloaded = chats.reload(
        "user-1", session_id, str(edited_assistant["id"]), idempotency_key="reload-1",
        respond=lambda request: (_knowledge(contexts.append(request["messages"]) or "重新回答")),
    )
    reloaded_assistant = reloaded["messages"][-1]
    assert reloaded_assistant["id"] != edited_assistant["id"]
    assert reloaded_assistant["parent_id"] == edited_user["id"]
    assert reloaded_assistant["reply_to_id"] == edited_user["id"]
    assert contexts[-1] == [{"role": "user", "content": "修改后的问题"}]
    assert [item["id"] for item in reloaded["messages"] if item["is_current_branch"]] == [edited_user["id"], reloaded_assistant["id"]]
    assert reloaded["current_branch_head_id"] == reloaded_assistant["id"]

    switched = chats.switch_branch("user-1", session_id, str(original_assistant["id"]), idempotency_key="switch-1")
    assert switched["current_branch_head_id"] == original_assistant["id"]
    assert [item["id"] for item in switched["messages"] if item["is_current_branch"]] == [original_user["id"], original_assistant["id"]]
    assert [event["type"] for event in chats.list_events("user-1", session_id)["events"]].count("branch.created") == 2
    assert "branch.switched" in [event["type"] for event in chats.list_events("user-1", session_id)["events"]]
    assert chats.switch_branch("user-1", session_id, str(original_assistant["id"]), idempotency_key="switch-1") == switched
    assert chats.edit(
        "user-1", session_id, str(original_user["id"]), content="修改后的问题", idempotency_key="edit-1",
        respond=lambda _: pytest.fail("idempotent edit must not call the model"),
    ) == edited
    with pytest.raises(ChatError) as conflict:
        chats.edit(
            "user-1", session_id, str(original_user["id"]), content="冲突内容", idempotency_key="edit-1",
            respond=lambda _: pytest.fail("conflicting edit must not call the model"),
        )
    assert conflict.value.code == "idempotency_conflict"
    with pytest.raises(ChatError) as hidden_owner:
        chats.switch_branch("user-2", session_id, str(original_assistant["id"]), idempotency_key="other-owner")
    assert hidden_owner.value.code == "chat_not_found"


def test_reload_failure_keeps_original_and_creates_replayable_failed_branch(tmp_path: Path) -> None:
    chats = ChatSessions(tmp_path / "gateway.db")
    session_id = str(chats.create("user-1", idempotency_key="create-1")["id"])
    first = chats.send("user-1", session_id, content="问题", idempotency_key="message-1", respond=lambda _: _knowledge("原回答"))
    original = first["messages"][-1]

    def unavailable(_request: dict[str, object]) -> dict[str, object]:
        raise RuntimeError("model unavailable")

    failed = chats.reload(
        "user-1", session_id, str(original["id"]), idempotency_key="reload-failed", respond=unavailable,
    )
    failed_assistant = failed["messages"][-1]
    assert original["status"] == "completed"
    assert failed_assistant["id"] != original["id"]
    assert failed_assistant["status"] == "failed"
    assert failed_assistant["error_code"] == "model_unavailable"
    assert chats.reload(
        "user-1", session_id, str(original["id"]), idempotency_key="reload-failed",
        respond=lambda _: pytest.fail("idempotent reload must not call the model"),
    ) == failed


def test_controlled_diagnosis_error_is_preserved_on_failed_message(tmp_path: Path) -> None:
    chats = ChatSessions(tmp_path / "gateway.db")
    session_id = str(chats.create("user-1", idempotency_key="create")["id"])

    def unsupported(_request: dict[str, object]) -> dict[str, object]:
        raise ChatError("image_input_unsupported", "当前模型不支持图片附件")

    with pytest.raises(ChatError) as rejected:
        chats.send(
            "user-1", session_id, content="分析图片", idempotency_key="message",
            respond=unsupported,
        )
    assert rejected.value.code == "image_input_unsupported"
    failed = chats.get("user-1", session_id)["messages"][-1]
    assert failed["status"] == "failed"
    assert failed["error_code"] == "image_input_unsupported"
    assert failed["content"] == "当前模型不支持图片附件"


def test_branch_operations_reject_hidden_siblings_and_running_heads(tmp_path: Path) -> None:
    chats = ChatSessions(tmp_path / "gateway.db")
    session_id = str(chats.create("user-1", idempotency_key="create-1")["id"])
    first = chats.send("user-1", session_id, content="问题", idempotency_key="message-1", respond=lambda _: _knowledge("回答"))
    original_assistant = str(first["messages"][-1]["id"])
    edited = chats.edit(
        "user-1", session_id, str(first["messages"][0]["id"]), content="另一个问题", idempotency_key="edit-1",
        respond=lambda _: _knowledge("另一个回答"),
    )
    with pytest.raises(ChatError) as hidden:
        chats.reload("user-1", session_id, original_assistant, idempotency_key="reload-hidden", respond=lambda _: _knowledge("不要调用"))
    assert hidden.value.code == "chat_message_not_found"
    started = Event()
    release = Event()
    def blocking(_request: dict[str, object]) -> dict[str, object]:
        started.set()
        release.wait(2)
        return _knowledge("重新回答")

    worker = Thread(
        target=lambda: chats.reload(
            "user-1", session_id, str(edited["messages"][-1]["id"]),
            idempotency_key="reload-running", respond=blocking,
        ),
    )
    worker.start()
    assert started.wait(2)
    with pytest.raises(ChatError) as running:
        chats.switch_branch("user-1", session_id, original_assistant, idempotency_key="switch-running")
    assert running.value.code == "message_running"
    release.set()
    worker.join(2)
