"""Explicit Chat Handoff domain contracts."""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from apps.aiops_k8s_gateway.chat_handoffs import ChatHandoffError, ChatHandoffs
from apps.aiops_k8s_gateway.chat_scope import freeze_chat_scope
from apps.aiops_k8s_gateway.chat_sessions import ChatSessions
from apps.aiops_k8s_gateway.connector_identity import ConnectorIdentity
from apps.aiops_k8s_gateway.incident import AlertSignal, IncidentService
from apps.aiops_k8s_gateway.investigation_events import InvestigationEvents
from apps.aiops_k8s_gateway.resource_catalog import DiscoveryObservation, ResourceCatalog
from apps.aiops_k8s_gateway.v1_store import GatewayV1Store


def _knowledge(answer: str) -> dict[str, object]:
    return {
        "mode": "knowledge", "answer": answer, "scope": None, "tool_activity": [],
        "evidence_references": [], "uncertainty": None, "next_step": None,
        "completion": {"status": "completed", "stopping_reason": "knowledge_answered"},
    }


def _existing_incident(tmp_path: Path) -> tuple[GatewayV1Store, str, str]:
    store = GatewayV1Store(tmp_path / "gateway.db", credential_factory=lambda: "connector-secret")
    _, credential = store.connector_enrollments.create(
        connector_id="connector-prod", cluster_id="cluster-prod", actor_id="admin",
        reason="test", request_id="enroll-1",
    )
    store.connector_enrollments.register(credential, "connector-prod", "cluster-prod", request_id="register-1")
    ids = itertools.count(1)
    incidents = IncidentService(
        store.database, ResourceCatalog(store.database), ConnectorIdentity(store.database),
        clock=lambda: 1_000.0, id_factory=lambda prefix: f"{prefix}-{next(ids)}",
    )
    incident = incidents.ingest(AlertSignal(
        fingerprint="fp-1", alertname="HighErrorRate", status="firing", severity="critical",
        cluster_id="cluster-prod", namespace="shop",
    ))["incident"]
    incident_id = str(incident["id"])  # type: ignore[index]
    workbench = incidents.workbench(incident_id, team_ids=None, actor_capabilities=["manage_investigation"])
    assert workbench is not None
    return store, incident_id, str(workbench["investigation"]["id"])  # type: ignore[index]


def test_selected_messages_handoff_to_existing_incident_is_human_input_and_idempotent(tmp_path: Path) -> None:
    store, incident_id, investigation_id = _existing_incident(tmp_path)
    chat_ids = itertools.count(1)
    chats = ChatSessions(store.database, clock=lambda: 1_000.0, id_factory=lambda: f"chat-object-{next(chat_ids)}")
    session_id = str(chats.create("user-1", idempotency_key="create-chat")["id"])
    chats.send(
        "user-1", session_id, content="发布后错误率升高", idempotency_key="message-1",
        respond=lambda _request: _knowledge("先检查指标与日志。"),
    )
    chat = chats.send(
        "user-1", session_id, content="只转交这一轮", idempotency_key="message-2",
        respond=lambda _request: _knowledge("已记录待核实上下文。"),
    )
    selected_ids = [str(chat["messages"][0]["id"]), str(chat["messages"][3]["id"])]  # type: ignore[index]
    handoffs = ChatHandoffs(
        store.database, clock=lambda: 1_001.0, id_factory=lambda prefix: f"{prefix}-1",
    )

    result = handoffs.execute(
        actor_id="user-1", session_id=session_id, message_ids=selected_ids,
        idempotency_key="handoff-1", team_ids=None, target_incident_id=incident_id,
    )

    assert result == {
        "id": "handoff-1", "chat_session_id": session_id, "target_type": "existing_incident",
        "incident_id": incident_id, "investigation_id": investigation_id,
        "selected_message_ids": selected_ids, "created_at": 1_001.0, "idempotent": False,
    }
    events = InvestigationEvents(store.database).list(investigation_id)["events"]
    transferred = [event for event in events if event["type"] == "human_input.assertion"]
    assert [event["payload"]["chat_message_id"] for event in transferred] == selected_ids
    assert [event["payload"]["content"] for event in transferred] == [
        "发布后错误率升高", "已记录待核实上下文。",
    ]
    assert all(event["actor_id"] == "user-1" and event["payload"]["source"] == "chat_handoff" for event in transferred)
    assert not any("evidence" in event["type"] or "approval" in event["type"] for event in events)
    assert chats.list_events("user-1", session_id)["events"][-1]["type"] == "handoff.completed"

    assert handoffs.execute(
        actor_id="user-1", session_id=session_id, message_ids=selected_ids,
        idempotency_key="handoff-1", team_ids=None, target_incident_id=incident_id,
    ) == {**result, "idempotent": True}
    assert len(InvestigationEvents(store.database).list(investigation_id)["events"]) == len(events)
    with pytest.raises(ChatHandoffError) as conflict:
        handoffs.execute(
            actor_id="user-1", session_id=session_id, message_ids=selected_ids[:1],
            idempotency_key="handoff-1", team_ids=None, target_incident_id=incident_id,
        )
    assert conflict.value.code == "idempotency_conflict"


def test_handoff_rejects_message_from_hidden_chat_branch(tmp_path: Path) -> None:
    store, incident_id, _ = _existing_incident(tmp_path)
    ids = itertools.count(1)
    chats = ChatSessions(store.database, id_factory=lambda: f"chat-{next(ids)}")
    session_id = str(chats.create("user-1", idempotency_key="create-chat")["id"])
    first = chats.send(
        "user-1", session_id, content="原问题", idempotency_key="message-1",
        respond=lambda _request: _knowledge("原回答"),
    )
    original_id = str(first["messages"][-1]["id"])
    chats.edit(
        "user-1", session_id, str(first["messages"][0]["id"]), content="新问题", idempotency_key="edit-1",
        respond=lambda _request: _knowledge("新回答"),
    )
    handoffs = ChatHandoffs(store.database)
    with pytest.raises(ChatHandoffError) as hidden:
        handoffs.execute(
            actor_id="user-1", session_id=session_id, message_ids=[original_id],
            idempotency_key="handoff-hidden", team_ids=None, target_incident_id=incident_id,
        )
    assert hidden.value.code == "chat_message_not_found"


def test_handoff_creates_scoped_user_incident_and_first_investigation_without_alert_signal(tmp_path: Path) -> None:
    store = GatewayV1Store(tmp_path / "gateway.db", credential_factory=lambda: "connector-secret")
    _, credential = store.connector_enrollments.create(
        connector_id="connector-prod", cluster_id="cluster-prod", actor_id="admin",
        reason="test", request_id="enroll-1",
    )
    store.connector_enrollments.register(credential, "connector-prod", "cluster-prod", request_id="register-1")
    _, team = store.mutate_admin(
        collection="teams", target_id=None, payload={"name": "Payments", "description": ""},
        actor_id="admin", reason="test", action="teams_create", request_id="team-1",
    )
    catalog = ResourceCatalog(store.database)
    [candidate] = catalog.refresh_discovery("cluster-prod", [
        DiscoveryObservation(namespace="shop", workload_kind="Deployment", workload_name="checkout-api"),
    ])
    service = catalog.create_service(
        team_id=str(team["id"]), name="Checkout", description="", actor_id="admin",
        reason="test", request_id="service-1",
    )
    binding = catalog.confirm_binding(
        candidate_id=str(candidate["id"]), service_id=str(service["id"]), actor_id="admin",
        reason="test", request_id="binding-1",
    )
    target_id = str(binding["deployment_target_id"])
    frozen_scope = freeze_chat_scope(
        {"deployment_target_id": target_id},
        workspace=catalog.list_for_actor(
            team_ids=None, connector_status=store.connector_enrollments.public_status(),
        ),
    )
    chat_ids = itertools.count(1)
    chats = ChatSessions(
        store.database, clock=lambda: 2_000.0,
        id_factory=lambda: f"chat-object-{next(chat_ids)}",
    )
    session_id = str(chats.create("admin", idempotency_key="create-chat")["id"])
    chat = chats.send(
        "admin", session_id, content="checkout 发布后持续报错", idempotency_key="message-1",
        respond=lambda _request: _knowledge("建议升级为正式 Investigation。"),
    )
    selected_id = str(chat["messages"][0]["id"])  # type: ignore[index]
    handoffs = ChatHandoffs(
        store.database, clock=lambda: 2_001.0,
        id_factory=lambda prefix: {"handoff": "handoff-new", "incident": "incident-new", "investigation": "investigation-new", "diagnosis-request": "diagnosis-new"}[prefix],
    )

    result = handoffs.execute(
        actor_id="admin", session_id=session_id, message_ids=[selected_id],
        idempotency_key="handoff-new", team_ids=None,
        problem_summary="checkout 发布后错误率持续升高", frozen_scope=frozen_scope,
    )

    assert result["target_type"] == "user_created_incident"
    assert result["incident_id"] == "incident-new"
    assert result["investigation_id"] == "investigation-new"
    incidents = IncidentService(
        store.database, catalog, ConnectorIdentity(store.database), clock=lambda: 2_001.0,
    )
    [incident] = incidents.list_incidents(team_ids=None)
    assert incident["origin"] == "user"
    assert incident["title"] == "checkout 发布后错误率持续升高"
    assert incident["binding_status"] == "bound"
    assert incident["signal_count"] == 0
    workbench = incidents.workbench("incident-new", team_ids=None, actor_capabilities=["manage_investigation"])
    assert workbench is not None
    assert workbench["resource_context"]["deployment_target_id"] == target_id  # type: ignore[index]
    assert workbench["investigation"]["status"] == "queued"  # type: ignore[index]
    events = InvestigationEvents(store.database).list("investigation-new")["events"]
    assert [event["type"] for event in events] == [
        "investigation.lifecycle", "handoff.created", "human_input.assertion",
    ]
    with store.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM alert_signals WHERE incident_id = 'incident-new'").fetchone()[0] == 0
        assert conn.execute("SELECT status FROM diagnosis_requests WHERE investigation_id = 'investigation-new'").fetchone()[0] == "pending"


def test_existing_incident_handoff_hides_unauthorized_targets_and_rejects_terminal_investigations(tmp_path: Path) -> None:
    store, incident_id, investigation_id = _existing_incident(tmp_path)
    _, team = store.mutate_admin(
        collection="teams", target_id=None, payload={"name": "Owners", "description": ""},
        actor_id="admin", reason="test", action="teams_create", request_id="team-1",
    )
    chat_ids = itertools.count(1)
    chats = ChatSessions(
        store.database, clock=lambda: 1_000.0,
        id_factory=lambda: f"chat-object-{next(chat_ids)}",
    )
    session_id = str(chats.create("user-1", idempotency_key="create-chat")["id"])
    chat = chats.send(
        "user-1", session_id, content="需要转交", idempotency_key="message-1",
        respond=lambda _request: _knowledge("待核实。"),
    )
    selected_id = str(chat["messages"][0]["id"])  # type: ignore[index]
    handoffs = ChatHandoffs(store.database, clock=lambda: 1_001.0, id_factory=lambda prefix: f"{prefix}-1")
    with store.database.connect() as conn:
        conn.execute("UPDATE incidents SET team_id = ? WHERE id = ?", (team["id"], incident_id))

    codes = []
    for target_id, team_ids in ((incident_id, set()), ("missing", None)):
        with pytest.raises(ChatHandoffError) as error:
            handoffs.execute(
                actor_id="user-1", session_id=session_id, message_ids=[selected_id],
                idempotency_key=f"handoff-{target_id}", team_ids=team_ids,
                target_incident_id=target_id,
            )
        codes.append(error.value.code)
    assert codes == ["handoff_target_not_found", "handoff_target_not_found"]

    with store.database.connect() as conn:
        conn.execute("UPDATE investigations SET status = 'completed' WHERE id = ?", (investigation_id,))
    with pytest.raises(ChatHandoffError) as terminal:
        handoffs.execute(
            actor_id="user-1", session_id=session_id, message_ids=[selected_id],
            idempotency_key="handoff-terminal", team_ids={str(team["id"])},
            target_incident_id=incident_id,
        )
    assert terminal.value.code == "investigation_terminal"
