"""测试异步审批模块。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest


def _load_module(tmp_path: Path):
    """按文件路径加载模块，并重定向数据库路径。"""
    module_path = Path(__file__).resolve().parents[1] / "toolsets" / "approval_async.py"
    spec = importlib.util.spec_from_file_location("test_approval_async_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module._DB.close()
    module._DB = module.ApprovalDB(tmp_path / "approvals.db")
    return module


def test_load_config_prefers_hermes_config_over_hermes_home(
    tmp_path: Path,
    monkeypatch,
    **_kwargs,
) -> None:
    """运行时配置应优先读取 HERMES_CONFIG，再回退 HERMES_HOME/config.yaml。"""
    module = _load_module(tmp_path)
    explicit_config = tmp_path / "explicit.yaml"
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    explicit_config.write_text(
        "platforms:\n  feishu:\n    main_chat_id: oc_explicit\n",
        encoding="utf-8",
    )
    (hermes_home / "config.yaml").write_text(
        "platforms:\n  feishu:\n    main_chat_id: oc_home\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_CONFIG", str(explicit_config))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    assert module._load_config_sync()["platforms"]["feishu"]["main_chat_id"] == "oc_explicit"


def test_load_config_falls_back_to_hermes_home_config(
    tmp_path: Path,
    monkeypatch,
    **_kwargs,
) -> None:
    """未设置 HERMES_CONFIG 时，应读取 HERMES_HOME/config.yaml。"""
    module = _load_module(tmp_path)
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "platforms:\n  feishu:\n    main_chat_id: oc_home\n",
        encoding="utf-8",
    )

    monkeypatch.delenv("HERMES_CONFIG", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    assert module._load_config_sync()["platforms"]["feishu"]["main_chat_id"] == "oc_home"


def test_load_config_without_env_does_not_read_repo_root_config(
    tmp_path: Path,
    monkeypatch,
    **_kwargs,
) -> None:
    """未设置运行时 env 时，即使 CWD 有 config.yaml 也不得读取。"""
    module = _load_module(tmp_path)
    cwd = tmp_path / "repo"
    cwd.mkdir()
    (cwd / "config.yaml").write_text(
        "platforms:\n  feishu:\n    main_chat_id: oc_repo_root\n",
        encoding="utf-8",
    )

    monkeypatch.delenv("HERMES_CONFIG", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    old_cwd = Path.cwd()
    os.chdir(cwd)
    try:
        assert module._load_config_sync() == {}
    finally:
        os.chdir(old_cwd)


@pytest.mark.asyncio
async def test_approval_lifecycle(tmp_path: Path, **_kwargs) -> None:
    """审批应支持完整生命周期。"""
    module = _load_module(tmp_path)

    approval_id = await module.request_approval(
        "k8s_write",
        "kubectl apply -f deploy.yaml",
        {"env": "prod"},
        "default",
        "alice",
        "standard",
    )
    checked = await module.check_approval(approval_id)
    resolved = await module.resolve_approval(approval_id, "approved", "bob")
    executed = await module.execute_approved(approval_id)
    final_state = await module.check_approval(approval_id)

    assert checked["status"] == "pending"
    assert resolved["status"] == "approved"
    assert executed["status"] == "executed"
    assert final_state["status"] == "executed"
    assert final_state["approver"] == "bob"
    assert final_state["result"]["executed"] is True


@pytest.mark.asyncio
async def test_expire_stale_pending_approval(tmp_path: Path, **_kwargs) -> None:
    """超时未处理审批应被标记为 expired。"""
    module = _load_module(tmp_path)

    approval_id = await module.request_approval(
        "k8s_exec",
        "kubectl exec pod/app -- sh",
        {},
        "ops",
        "alice",
        "elevated",
    )
    module._DB._conn.execute("UPDATE approvals SET created_at = ? WHERE id = ?", (0, approval_id))
    expired = await module.expire_stale(timeout_minutes=30)
    checked = await module.check_approval(approval_id)

    assert expired["ok"] is True
    assert expired["expired"] == 1
    assert expired["approvals"] == [{"approval_id": approval_id, "incident_id": None}]
    assert checked["status"] == "expired"


@pytest.mark.asyncio
async def test_denied_flow_cannot_execute(tmp_path: Path, **_kwargs) -> None:
    """被拒绝的审批不允许执行。"""
    module = _load_module(tmp_path)

    approval_id = await module.request_approval(
        "k8s_write",
        "kubectl delete deployment web",
        {},
        "prod",
        "alice",
        "dangerous",
    )
    resolved = await module.resolve_approval(approval_id, "denied", "bob")
    executed = await module.execute_approved(approval_id)

    assert resolved["status"] == "denied"
    assert executed["ok"] is False
    assert "当前状态不允许执行" in executed["message"]


@pytest.mark.asyncio
async def test_approval_records_incident_and_message_ids(tmp_path: Path, **_kwargs) -> None:
    """审批记录应关联 incident 与飞书审批消息。"""
    module = _load_module(tmp_path)

    approval_id = await module.request_approval(
        "k8s_write",
        "kubectl rollout restart deployment/web -n prod",
        {"resource": "deployment/web"},
        "prod",
        "alice",
        "dangerous",
        incident_id="incident-1",
        approval_message_id="om_approval",
    )
    checked = await module.check_approval(approval_id)

    assert checked["incident_id"] == "incident-1"
    assert checked["approval_message_id"] == "om_approval"


@pytest.mark.asyncio
async def test_find_pending_by_incident_and_signature(tmp_path: Path, **_kwargs) -> None:
    """同一 incident/action signature 应复用已有 pending approval。"""
    module = _load_module(tmp_path)
    approval_id = await module.request_approval(
        "k8s_write",
        "检查并重启 deployment/nginx",
        {"action_signature": "restart:default:deployment/nginx"},
        "default",
        "alert_webhook",
        "standard",
        incident_id="inc-1",
    )

    found = await module.find_pending_approval(
        incident_id="inc-1",
        action_signature="restart:default:deployment/nginx",
    )

    assert found is not None
    assert found["approval_id"] == approval_id
    assert found["status"] == "pending"


@pytest.mark.asyncio
async def test_update_approval_message_id_writes_back(tmp_path: Path, **_kwargs) -> None:
    """approval_message_id 应能写回审批记录。"""
    module = _load_module(tmp_path)
    approval_id = await module.request_approval(
        "k8s_write",
        "kubectl rollout restart deployment/web",
        {"action_signature": "restart:default:deployment/web"},
        "default",
        "alert_webhook",
        "standard",
        incident_id="inc-1",
    )

    updated = await module.update_approval_message_id(approval_id, "om_approval")
    checked = await module.check_approval(approval_id)

    assert updated == {
        "ok": True,
        "approval_id": approval_id,
        "approval_message_id": "om_approval",
    }
    assert checked["approval_message_id"] == "om_approval"


@pytest.mark.asyncio
async def test_publish_or_queue_approval_card_updates_message_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    **_kwargs,
) -> None:
    """审批投递成功后应写回 approval_message_id 并标记 delivery sent。"""
    module = _load_module(tmp_path)
    approval_id = await module.request_approval(
        "k8s_write",
        "kubectl rollout restart deployment/web",
        {"action_signature": "restart:default:deployment/web"},
        "default",
        "alert_webhook",
        "standard",
        incident_id="inc-1",
    )

    calls: dict[str, list[tuple]] = {"upsert": [], "sent": [], "failed": [], "card": []}

    async def _get_incident(incident_id: str) -> dict:
        assert incident_id == "inc-1"
        return {
            "id": "inc-1",
            "platform": "feishu",
            "chat_id": "oc_ops",
            "root_message_id": "om_root",
            "thread_id": "omt_thread",
            "status_card_message_id": "om_card",
        }

    async def _publish_approval_card(approval: dict, incident: dict, config: dict) -> dict:
        calls["card"].append((approval, incident, config))
        return {"message_id": "om_approval", "root_message_id": "om_root", "thread_id": "omt_thread"}

    async def _upsert_delivery(**kwargs):
        calls["upsert"].append(kwargs)
        return "delivery-1"

    async def _mark_sent(delivery_id: str, target_message_id: str) -> None:
        calls["sent"].append((delivery_id, target_message_id))

    async def _mark_failed(delivery_id: str, error: str) -> None:
        calls["failed"].append((delivery_id, error))

    monkeypatch.setattr(module.incident_store, "get_incident", _get_incident)
    monkeypatch.setattr(module.feishu_conversation, "publish_approval_card", _publish_approval_card)
    monkeypatch.setattr(module.message_delivery, "upsert_delivery", _upsert_delivery)
    monkeypatch.setattr(module.message_delivery, "mark_sent", _mark_sent)
    monkeypatch.setattr(module.message_delivery, "mark_failed", _mark_failed)

    result = await module.publish_or_queue_approval_card(approval_id, config={})
    checked = await module.check_approval(approval_id)

    assert result["delivery_status"] == "sent"
    assert result["approval_message_id"] == "om_approval"
    assert checked["approval_message_id"] == "om_approval"
    assert calls["upsert"][0]["target_type"] == "approval_card"
    assert calls["sent"] == [("delivery-1", "om_approval")]
    assert calls["failed"] == []


@pytest.mark.asyncio
async def test_publish_or_queue_approval_card_backfills_existing_sent_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    **_kwargs,
) -> None:
    """已有 sent approval_card 投递时，应补回写 approval_message_id 且不重复发卡。"""
    module = _load_module(tmp_path)
    module.message_delivery._DB.close()
    module.message_delivery._DB = module.message_delivery.MessageDeliveryDB(
        tmp_path / "message_deliveries.db"
    )
    approval_id = await module.request_approval(
        "k8s_write",
        "kubectl rollout restart deployment/web",
        {"action_signature": "restart:default:deployment/web"},
        "default",
        "alert_webhook",
        "standard",
        incident_id="inc-1",
    )
    delivery_id = await module.message_delivery.upsert_delivery(
        incident_id="inc-1",
        approval_id=approval_id,
        target_type="approval_card",
        platform="feishu",
        chat_id="oc_ops",
        thread_id="omt_thread",
        payload_hash="existing-approval-card",
    )
    await module.message_delivery.mark_sent(delivery_id, "om_existing_approval")
    publish_calls: list[tuple[dict, dict, dict]] = []

    async def _publish_approval_card(approval: dict, incident: dict, config: dict) -> dict:
        publish_calls.append((approval, incident, config))
        return {"message_id": "om_should_not_send"}

    monkeypatch.setattr(
        module.feishu_conversation,
        "publish_approval_card",
        _publish_approval_card,
    )

    result = await module.publish_or_queue_approval_card(approval_id, config={})
    checked = await module.check_approval(approval_id)

    assert result["delivery_status"] == "sent"
    assert result["approval_message_id"] == "om_existing_approval"
    assert result["delivery_id"] == delivery_id
    assert checked["approval_message_id"] == "om_existing_approval"
    assert publish_calls == []


@pytest.mark.asyncio
async def test_request_handler_returns_card_delivery_contract(tmp_path: Path, **_kwargs) -> None:
    """工具入口成功投递审批卡片时，应返回审批与投递契约字段。"""
    module = _load_module(tmp_path)

    async def _publish_or_queue_approval_card(approval_id: str, *, config=None):
        assert config == {"platforms": {"feishu": {"enabled": True}}}
        return {
            "ok": True,
            "approval_id": approval_id,
            "approval_message_id": "om_approval",
            "delivery_status": "sent",
        }

    module.publish_or_queue_approval_card = _publish_or_queue_approval_card  # type: ignore[assignment]

    raw_result = await module._request_handler(
        {
            "operation_type": "k8s_write",
            "command": "kubectl rollout restart deployment/web",
            "context": {"action_signature": "restart:default:deployment/web"},
            "namespace": "default",
            "requester": "alert_webhook",
            "risk_level": "standard",
            "incident_id": "inc-1",
        },
        config={"platforms": {"feishu": {"enabled": True}}},
    )

    result = json.loads(raw_result)

    assert result["ok"] is True
    assert result["approval_id"]
    assert result["approval_message_id"] == "om_approval"
    assert result["delivery_status"] == "sent"


@pytest.mark.asyncio
async def test_recover_pending_approval_cards_scans_old_rows(tmp_path: Path, **_kwargs) -> None:
    """旧的 pending 且无 approval_message_id 审批应进入补发扫描。"""
    module = _load_module(tmp_path)
    old_approval_id = await module.request_approval(
        "k8s_write",
        "kubectl rollout restart deployment/web",
        {"action_signature": "restart:default:deployment/web-old"},
        "default",
        "alert_webhook",
        "standard",
        incident_id="inc-old",
    )
    fresh_approval_id = await module.request_approval(
        "k8s_write",
        "kubectl rollout restart deployment/api",
        {"action_signature": "restart:default:deployment/api-fresh"},
        "default",
        "alert_webhook",
        "standard",
        incident_id="inc-fresh",
    )
    module._DB._conn.execute("UPDATE approvals SET created_at = 0 WHERE id = ?", (old_approval_id,))
    module._DB._conn.execute("UPDATE approvals SET created_at = 9999999999 WHERE id = ?", (fresh_approval_id,))

    calls: list[str] = []

    async def _publish_or_queue_approval_card(approval_id: str, *, config=None):
        del config
        calls.append(approval_id)
        return {
            "ok": True,
            "approval_id": approval_id,
            "approval_message_id": "om_approval" if approval_id == old_approval_id else None,
            "delivery_status": "sent" if approval_id == old_approval_id else "pending_retry",
        }

    module.publish_or_queue_approval_card = _publish_or_queue_approval_card  # type: ignore[assignment]

    result = await module.recover_pending_approval_cards(timeout_seconds=60)

    assert calls == [old_approval_id]
    assert result["sent"] == 1
    assert result["pending_retry"] == 0
    assert result["scanned"] == 1


@pytest.mark.asyncio
async def test_concurrent_requests_generate_distinct_ids(tmp_path: Path, **_kwargs) -> None:
    """并发创建审批请求时应得到不同 ID。"""
    module = _load_module(tmp_path)

    async def _create_one(index: int) -> str:
        return await module.request_approval(
            "k8s_write",
            f"kubectl scale deployment/app --replicas={index}",
            {"index": index},
            "default",
            f"user-{index}",
            "standard",
        )

    ids = await asyncio.gather(*[_create_one(index) for index in range(10)])

    assert len(ids) == 10
    assert len(set(ids)) == 10


def test_approval_db_migrates_external_approval_fields(tmp_path: Path) -> None:
    """旧 approvals 表迁移后应具备飞书原生审批字段，历史记录仍可读。"""
    db_path = tmp_path / "legacy-approvals.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE approvals (
            id TEXT PRIMARY KEY,
            operation_type TEXT NOT NULL,
            command TEXT NOT NULL,
            context_json TEXT,
            namespace TEXT,
            requester TEXT,
            approver TEXT,
            incident_id TEXT,
            approval_message_id TEXT,
            status TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            created_at REAL NOT NULL,
            decided_at REAL,
            executed_at REAL,
            result_json TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO approvals (
            id, operation_type, command, context_json, namespace, requester,
            approver, incident_id, approval_message_id, status, risk_level,
            created_at, decided_at, executed_at, result_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "legacy-ap-1",
            "k8s_write",
            "kubectl rollout restart deployment/nginx",
            "{}",
            "default",
            "alice",
            None,
            "inc-1",
            "om_old",
            "pending",
            "low",
            1.0,
            None,
            None,
            None,
        ),
    )
    conn.commit()
    conn.close()

    module_path = Path(__file__).resolve().parents[1] / "toolsets" / "approval_async.py"
    spec = importlib.util.spec_from_file_location("test_approval_async_migration_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module._DB.close()
    module._DB = module.ApprovalDB(db_path)

    columns = {
        row[1]
        for row in module._DB._conn.execute("PRAGMA table_info(approvals)").fetchall()
    }
    checked = module._DB.check_approval("legacy-ap-1")

    assert {
        "external_provider",
        "external_uuid",
        "external_approval_code",
        "external_instance_code",
        "external_status",
        "external_url",
        "external_created_at",
        "external_updated_at",
        "external_last_error",
        "external_poll_attempts",
        "external_next_poll_at",
    }.issubset(columns)
    assert checked["found"] is True
    assert checked["approval_id"] == "legacy-ap-1"
    assert checked["approval_message_id"] == "om_old"


@pytest.mark.asyncio
async def test_native_approval_create_success_sets_external_pending(tmp_path: Path, **_kwargs) -> None:
    """原生审批创建成功后应写回 instance_code、链接和 external_pending 状态。"""
    module = _load_module(tmp_path)
    approval_id = await module.request_approval(
        "k8s_write",
        "kubectl rollout restart deployment/nginx",
        {"action_signature": "restart_deployment:prod-a:default:deployment/nginx"},
        "default",
        "alert_webhook",
        "low",
        incident_id="inc-1",
    )
    record_external = getattr(module, "record_external_approval_created", None)
    assert callable(record_external), "record_external_approval_created(...) is required"

    recorded = await record_external(
        approval_id,
        provider="feishu",
        external_uuid=approval_id,
        external_approval_code="approval-code",
        external_instance_code="INST-001",
        external_status="PENDING",
        external_url="https://approval.feishu.cn/approval/INST-001",
    )
    checked = await module.check_approval(approval_id)

    assert recorded["ok"] is True
    assert recorded["status"] == "external_pending"
    assert checked["status"] == "external_pending"
    assert checked["external_provider"] == "feishu"
    assert checked["external_uuid"] == approval_id
    assert checked["external_instance_code"] == "INST-001"
    assert checked["external_url"] == "https://approval.feishu.cn/approval/INST-001"


@pytest.mark.asyncio
async def test_native_approval_create_failure_sets_create_failed(tmp_path: Path, **_kwargs) -> None:
    """原生审批创建失败应进入 approval_create_failed，不能继续作为待执行审批。"""
    module = _load_module(tmp_path)
    approval_id = await module.request_approval(
        "k8s_write",
        "kubectl rollout restart deployment/nginx",
        {"action_signature": "restart_deployment:prod-a:default:deployment/nginx"},
        "default",
        "alert_webhook",
        "low",
        incident_id="inc-1",
    )
    record_failure = getattr(module, "record_external_approval_create_failed", None)
    assert callable(record_failure), "record_external_approval_create_failed(...) is required"

    result = await record_failure(
        approval_id,
        provider="feishu",
        error_type="feishu_error",
        message="approval_code not found",
    )
    checked = await module.check_approval(approval_id)
    executed = await module.execute_approved(approval_id)

    assert result["ok"] is True
    assert checked["status"] == "approval_create_failed"
    assert checked["external_last_error"]["error_type"] == "feishu_error"
    assert executed["ok"] is False
    assert "approval_create_failed" in executed["message"]


@pytest.mark.parametrize(
    ("external_status", "expected_status"),
    [("APPROVED", "approved"), ("REJECTED", "denied"), ("CANCELED", "canceled")],
)
@pytest.mark.asyncio
async def test_resolve_external_approval_maps_feishu_statuses(
    tmp_path: Path,
    external_status: str,
    expected_status: str,
    **_kwargs,
) -> None:
    """外部审批状态只能通过统一入口幂等同步到本地状态。"""
    module = _load_module(tmp_path)
    approval_id = await module.request_approval(
        "k8s_write",
        "kubectl rollout restart deployment/nginx",
        {"action_signature": "restart_deployment:prod-a:default:deployment/nginx"},
        "default",
        "alert_webhook",
        "low",
        incident_id="inc-1",
    )
    module._DB._conn.execute(
        """
        UPDATE approvals
        SET status = 'external_pending',
            external_provider = 'feishu',
            external_uuid = ?,
            external_instance_code = ?,
            external_status = 'PENDING'
        WHERE id = ?
        """,
        (approval_id, "INST-001", approval_id),
    )

    result = await module.resolve_external_approval(
        external_uuid=approval_id,
        external_instance_code="INST-001",
        external_status=external_status,
        source="feishu_event",
        raw_event={"event_id": "event-1"},
    )
    repeated = await module.resolve_external_approval(
        external_uuid=approval_id,
        external_instance_code="INST-001",
        external_status=external_status,
        source="feishu_event",
        raw_event={"event_id": "event-1-duplicate"},
    )
    checked = await module.check_approval(approval_id)

    assert result["ok"] is True
    assert result["status"] == expected_status
    assert repeated["ok"] is True
    assert repeated["status"] == expected_status
    assert checked["status"] == expected_status
    assert checked["external_status"] == external_status


@pytest.mark.asyncio
async def test_resolve_external_approval_ignores_local_pending_without_external_binding(
    tmp_path: Path,
    **_kwargs,
) -> None:
    """普通本地 pending 审批不能被飞书外部事件或 polling 直接推进。"""
    module = _load_module(tmp_path)
    approval_id = await module.request_approval(
        "k8s_write",
        "kubectl rollout restart deployment/nginx",
        {"action_signature": "restart_deployment:prod-a:default:deployment/nginx"},
        "default",
        "alert_webhook",
        "low",
        incident_id="inc-1",
    )

    result = await module.resolve_external_approval(
        external_uuid=approval_id,
        external_instance_code="INST-LOCAL-ONLY",
        external_status="APPROVED",
        source="feishu_event",
        raw_event={"event_id": "foreign-approved"},
    )
    checked = await module.check_approval(approval_id)

    assert result["ok"] is False
    assert result["status"] == "ignored"
    assert checked["status"] == "pending"
    assert checked["external_provider"] is None
    assert checked["external_uuid"] is None
    assert checked["external_instance_code"] is None


@pytest.mark.parametrize("terminal_status", ["executed", "failed", "denied", "canceled", "expired"])
@pytest.mark.asyncio
async def test_resolve_external_approval_does_not_override_terminal_statuses(
    tmp_path: Path,
    terminal_status: str,
    **_kwargs,
) -> None:
    """已执行或终态 approval 不能被外部事件回滚为 approved。"""
    module = _load_module(tmp_path)
    approval_id = await module.request_approval(
        "k8s_write",
        "kubectl rollout restart deployment/nginx",
        {"action_signature": "restart_deployment:prod-a:default:deployment/nginx"},
        "default",
        "alert_webhook",
        "low",
        incident_id="inc-1",
    )
    module._DB._conn.execute(
        """
        UPDATE approvals
        SET status = ?,
            external_provider = 'feishu',
            external_uuid = ?,
            external_instance_code = ?,
            external_status = 'PENDING'
        WHERE id = ?
        """,
        (terminal_status, approval_id, "INST-001", approval_id),
    )

    result = await module.resolve_external_approval(
        external_uuid=approval_id,
        external_instance_code="INST-001",
        external_status="APPROVED",
        source="feishu_event",
        raw_event={"event_id": "late-approved"},
    )
    checked = await module.check_approval(approval_id)

    assert result["ok"] is False
    assert result["status"] == terminal_status
    assert checked["status"] == terminal_status


@pytest.mark.parametrize(
    ("initial_status", "late_external_status"),
    [
        ("approval_create_failed", "APPROVED"),
        ("approved", "REJECTED"),
        ("approved", "CANCELED"),
    ],
)
@pytest.mark.asyncio
async def test_resolve_external_approval_preserves_failed_create_and_approved_terminal_states(
    tmp_path: Path,
    initial_status: str,
    late_external_status: str,
    **_kwargs,
) -> None:
    """迟到 webhook/polling 不得把创建失败或已批准状态改写成其他终态。"""
    module = _load_module(tmp_path)
    approval_id = await module.request_approval(
        "k8s_write",
        "kubectl rollout restart deployment/nginx",
        {"action_signature": "restart_deployment:prod-a:default:deployment/nginx"},
        "default",
        "alert_webhook",
        "low",
        incident_id="inc-1",
    )
    module._DB._conn.execute(
        """
        UPDATE approvals
        SET status = ?,
            external_provider = 'feishu',
            external_uuid = ?,
            external_instance_code = ?,
            external_status = 'PENDING'
        WHERE id = ?
        """,
        (initial_status, approval_id, "INST-001", approval_id),
    )

    result = await module.resolve_external_approval(
        external_uuid=approval_id,
        external_instance_code="INST-001",
        external_status=late_external_status,
        source="feishu_polling",
        raw_event={"event_id": "late-event"},
    )
    checked = await module.check_approval(approval_id)

    assert result["ok"] is False
    assert result["status"] == initial_status
    assert checked["status"] == initial_status


@pytest.mark.asyncio
async def test_only_local_approved_enters_execution_worker(tmp_path: Path, **_kwargs) -> None:
    """execution worker 只能扫描本地 approved，不能执行 external_pending 或 create_failed。"""
    module = _load_module(tmp_path)
    execution_path = Path(__file__).resolve().parents[1] / "toolsets" / "approval_execution.py"
    spec = importlib.util.spec_from_file_location("test_approval_execution_for_native_gate", execution_path)
    execution_module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = execution_module
    spec.loader.exec_module(execution_module)
    execution_module.approval_async = module
    execution_module._STORE = execution_module._ExecutionStore()

    approved_id = await module.request_approval(
        "k8s_write",
        "kubectl rollout restart deployment/nginx",
        {
            "action_signature": "restart_deployment:prod-a:default:deployment/nginx",
            "executable": True,
            "remediation_action": {
                "action_schema_version": "remediation.action.v1",
                "action_signature": "restart_deployment:prod-a:default:deployment/nginx",
                "action_type": "restart_deployment",
                "cluster": "prod-a",
                "namespace": "default",
                "resource_kind": "deployment",
                "resource_name": "nginx",
                "parameters": {"strategy": "rollout_restart"},
                "risk": {"risk_level": "low", "operation_type": "k8s_write"},
            },
        },
        "default",
        "alert_webhook",
        "low",
        incident_id="inc-1",
    )
    external_pending_id = await module.request_approval(
        "k8s_write",
        "kubectl rollout restart deployment/api",
        {"action_signature": "restart_deployment:prod-a:default:deployment/api"},
        "default",
        "alert_webhook",
        "low",
        incident_id="inc-2",
    )
    create_failed_id = await module.request_approval(
        "k8s_write",
        "kubectl rollout restart deployment/worker",
        {"action_signature": "restart_deployment:prod-a:default:deployment/worker"},
        "default",
        "alert_webhook",
        "low",
        incident_id="inc-3",
    )
    module._DB._conn.execute("UPDATE approvals SET status = 'approved', approver = 'ou_approver' WHERE id = ?", (approved_id,))
    module._DB._conn.execute(
        "UPDATE approvals SET status = 'external_pending', external_instance_code = 'INST-2' WHERE id = ?",
        (external_pending_id,),
    )
    module._DB._conn.execute(
        "UPDATE approvals SET status = 'approval_create_failed', external_last_error = ? WHERE id = ?",
        (json.dumps({"error_type": "token_error"}), create_failed_id),
    )

    queued = await asyncio.to_thread(execution_module._STORE.list_approved_without_execution, 10)
    result = await execution_module.process_pending_executions(
        limit=10,
        adapter=execution_module.NoopExecutionAdapter(),
    )

    assert queued == [approved_id]
    assert result["processed"] == 1
    assert result["results"][0]["approval_id"] == approved_id
