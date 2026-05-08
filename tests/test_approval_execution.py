"""Tests for approval execution coordinator idempotency and persistence."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


@pytest.fixture()
def approval_modules(tmp_path: Path):
    from toolsets import approval_async, approval_execution

    old_db = approval_async._DB
    approval_async._DB = approval_async.ApprovalDB(tmp_path / "approvals.db")
    try:
        yield approval_async, approval_execution
    finally:
        approval_async._DB.close()
        approval_async._DB = old_db


class RecordingAdapter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def dry_run_action(self, action, approval, execution):
        self.calls.append("dry_run")
        return {"ok": True, "stage": "dry_run", "action_type": action["action_type"]}

    async def acquire_lock(self, action, approval, execution):
        self.calls.append("lock")
        return {"ok": True, "lock_key": "lock-prod-default-nginx"}

    async def execute_action(self, action, approval, execution):
        self.calls.append("execute")
        return {"ok": True, "changed": True}

    async def record_audit(self, action, approval, execution, result):
        self.calls.append("audit")
        return {"ok": True, "audit_id": 41}

    async def check_health(self, action, approval, execution, result):
        self.calls.append("health")
        return {"ok": True, "checked": True}

    async def append_timeline(self, event_type, approval, execution):
        self.calls.append(f"timeline:{event_type}")

    async def notify(self, event_type, approval, execution):
        self.calls.append(f"notify:{event_type}")

    async def release_lock(self, action, approval, execution):
        self.calls.append("release")


class BlockingAdapter(RecordingAdapter):
    def __init__(self, started: asyncio.Event, release: asyncio.Event) -> None:
        super().__init__()
        self.started = started
        self.release = release

    async def dry_run_action(self, action, approval, execution):
        self.calls.append("dry_run")
        self.started.set()
        await self.release.wait()
        return {"ok": True, "stage": "dry_run", "action_type": action["action_type"]}


def _scale_context(namespace: str = "default", resource_name: str = "nginx") -> dict:
    signature = f"scale_deployment:prod-a:{namespace}:deployment/{resource_name}:replicas=3"
    return {
        "action_signature": signature,
        "executable": True,
        "remediation_action": {
            "action_schema_version": "remediation.action.v1",
            "action_signature": signature,
            "action_type": "scale_deployment",
            "cluster": "prod-a",
            "namespace": namespace,
            "resource_kind": "deployment",
            "resource_name": resource_name,
            "parameters": {"replicas": 3},
            "source": {
                "incident_id": "inc-1",
                "alertname": "ReplicaMismatch",
                "analysis_action": "scale deployment",
            },
            "risk": {"risk_level": "low", "operation_type": "k8s_write"},
        },
    }


async def _approved_approval(approval_async, *, incident_id: str | None = "inc-1", context: dict | None = None) -> str:
    approval_id = await approval_async.request_approval(
        "k8s_write",
        "scale deployment",
        context or _scale_context(),
        "default",
        "alert_webhook",
        "low",
        incident_id=incident_id,
    )
    resolved = await approval_async.resolve_approval(approval_id, "approved", "operator-1")
    assert resolved["ok"] is True
    return approval_id


@pytest.mark.asyncio
async def test_approved_executable_approval_executes_and_persists_status(approval_modules, **_kwargs) -> None:
    approval_async, approval_execution = approval_modules
    approval_id = await _approved_approval(approval_async)
    adapter = RecordingAdapter()

    result = await approval_execution.process_approval_execution(approval_id, adapter=adapter)
    execution = await approval_execution.check_execution(approval_id)
    approval = await approval_async.check_approval(approval_id)

    assert result["ok"] is True
    assert result["status"] == "succeeded"
    assert adapter.calls.count("execute") == 1
    assert approval["status"] == "executed"
    assert execution is not None
    assert execution["status"] == "succeeded"
    assert execution["action_signature"] == _scale_context()["action_signature"]
    assert execution["audit_id"] == 41
    assert execution["dry_run_result"]["stage"] == "dry_run"
    assert execution["health_result"]["checked"] is True
    assert execution["completed_at"] is not None


@pytest.mark.asyncio
async def test_process_pending_executions_respects_approved_after_cutoff(approval_modules, **_kwargs) -> None:
    approval_async, approval_execution = approval_modules
    historical_id = await _approved_approval(
        approval_async,
        context=_scale_context(resource_name="historical"),
    )
    eligible_id = await _approved_approval(
        approval_async,
        context=_scale_context(resource_name="eligible"),
    )
    cutoff = 1000.0

    with approval_async._DB._lock:
        approval_async._DB._conn.execute(
            "UPDATE approvals SET created_at = ?, decided_at = ? WHERE id = ?",
            (900.0, cutoff - 1.0, historical_id),
        )
        approval_async._DB._conn.execute(
            "UPDATE approvals SET created_at = ?, decided_at = ? WHERE id = ?",
            (900.0, cutoff + 1.0, eligible_id),
        )

    adapter = RecordingAdapter()
    result = await approval_execution.process_pending_executions(
        limit=10,
        adapter=adapter,
        approved_after=cutoff,
    )
    historical = await approval_async.check_approval(historical_id)
    eligible = await approval_async.check_approval(eligible_id)
    historical_execution = await approval_execution.check_execution(historical_id)
    eligible_execution = await approval_execution.check_execution(eligible_id)

    assert result["processed"] == 1
    assert adapter.calls.count("execute") == 1
    assert historical["status"] == "approved"
    assert historical_execution is None
    assert eligible["status"] == "executed"
    assert eligible_execution is not None
    assert eligible_execution["status"] == "succeeded"


@pytest.mark.asyncio
async def test_non_approved_approval_is_rejected_without_execution_record(approval_modules, **_kwargs) -> None:
    approval_async, approval_execution = approval_modules
    approval_id = await approval_async.request_approval(
        "k8s_write",
        "scale deployment",
        _scale_context(),
        "default",
        "alert_webhook",
        "low",
        incident_id="inc-1",
    )
    adapter = RecordingAdapter()

    result = await approval_execution.process_approval_execution(approval_id, adapter=adapter)
    execution = await approval_execution.check_execution(approval_id)

    assert result["ok"] is False
    assert result["reason_code"] == "approval_not_approved"
    assert adapter.calls == []
    assert execution is None


@pytest.mark.asyncio
async def test_missing_critical_context_fails_closed_and_records_failure(approval_modules, **_kwargs) -> None:
    approval_async, approval_execution = approval_modules
    approval_id = await _approved_approval(approval_async, incident_id=None)
    adapter = RecordingAdapter()

    result = await approval_execution.process_approval_execution(approval_id, adapter=adapter)
    execution = await approval_execution.check_execution(approval_id)

    assert result["ok"] is False
    assert result["reason_code"] == "approval_context_incomplete"
    assert adapter.calls == []
    assert execution is not None
    assert execution["status"] == "failed"
    assert "incident_id" in execution["error_message"]


@pytest.mark.asyncio
async def test_duplicate_processing_returns_existing_execution_without_second_execute(approval_modules, **_kwargs) -> None:
    approval_async, approval_execution = approval_modules
    approval_id = await _approved_approval(approval_async)
    adapter = RecordingAdapter()

    first = await approval_execution.process_approval_execution(approval_id, adapter=adapter)
    second = await approval_execution.process_approval_execution(approval_id, adapter=adapter)

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["reason_code"] == "already_executed"
    assert second["execution"]["id"] == first["execution"]["id"]
    assert adapter.calls.count("execute") == 1


@pytest.mark.asyncio
async def test_process_pending_without_adapter_fails_closed_without_marking_executed(
    approval_modules,
    **_kwargs,
) -> None:
    approval_async, approval_execution = approval_modules
    approval_id = await _approved_approval(approval_async)

    result = await approval_execution.process_pending_executions(limit=10)
    execution = await approval_execution.check_execution(approval_id)
    approval = await approval_async.check_approval(approval_id)

    assert result["processed"] == 1
    assert result["results"][0]["ok"] is False
    assert result["results"][0]["reason_code"] == "no_execution_adapter"
    assert result["results"][0]["status"] == "failed"
    assert approval["status"] == "approved"
    assert execution is not None
    assert execution["status"] == "failed"
    assert execution["error_message"] == "execution_adapter_missing"


@pytest.mark.asyncio
async def test_existing_queued_execution_is_claimed_once_without_second_pipeline(
    approval_modules,
    **_kwargs,
) -> None:
    approval_async, approval_execution = approval_modules
    approval_id = await _approved_approval(approval_async)
    approval = await approval_async.check_approval(approval_id)
    action = approval["context"]["remediation_action"]
    queued = await asyncio.to_thread(
        approval_execution._STORE.create_execution,
        approval,
        action,
        "queued",
        None,
    )
    assert queued["status"] == "queued"

    started = asyncio.Event()
    release = asyncio.Event()
    owner_adapter = BlockingAdapter(started, release)
    duplicate_adapter = RecordingAdapter()

    owner = asyncio.create_task(
        approval_execution.process_approval_execution(approval_id, adapter=owner_adapter)
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    duplicate = await approval_execution.process_approval_execution(
        approval_id,
        adapter=duplicate_adapter,
    )
    release.set()
    owned = await owner

    assert owned["ok"] is True
    assert duplicate["ok"] is False
    assert duplicate["reason_code"] == "already_processing"
    assert duplicate_adapter.calls == []
    assert owner_adapter.calls.count("dry_run") == 1
    assert owner_adapter.calls.count("execute") == 1


@pytest.mark.asyncio
async def test_forged_signature_is_recomputed_from_action_fields_before_execution(
    approval_modules,
    **_kwargs,
) -> None:
    approval_async, approval_execution = approval_modules
    context = _scale_context()
    context["remediation_action"]["parameters"]["replicas"] = 5
    approval_id = await _approved_approval(approval_async, context=context)
    adapter = RecordingAdapter()

    result = await approval_execution.process_approval_execution(approval_id, adapter=adapter)
    execution = await approval_execution.check_execution(approval_id)

    assert result["ok"] is False
    assert result["reason_code"] == "action_signature_mismatch"
    assert adapter.calls == []
    assert execution is not None
    assert execution["status"] == "failed"
    assert "structured fields" in execution["error_message"]


@pytest.mark.asyncio
async def test_execution_record_survives_db_reopen(approval_modules, tmp_path: Path, **_kwargs) -> None:
    approval_async, approval_execution = approval_modules
    db_path = tmp_path / "approvals.db"
    approval_id = await _approved_approval(approval_async)

    result = await approval_execution.process_approval_execution(approval_id, adapter=RecordingAdapter())
    approval_async._DB.close()
    approval_async._DB = approval_async.ApprovalDB(db_path)
    persisted = await approval_execution.check_execution(approval_id)

    assert result["status"] == "succeeded"
    assert persisted is not None
    assert persisted["status"] == "succeeded"
    assert persisted["approval_id"] == approval_id
    assert persisted["created_at"] <= persisted["updated_at"] <= persisted["completed_at"]
