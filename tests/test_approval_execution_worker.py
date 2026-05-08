"""Tests for the approval execution background worker."""

from __future__ import annotations

import sys
import threading
import types
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_tick_processes_pending_with_real_adapter_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tick should inject the remediation adapter into the coordinator."""
    from runtime import approval_execution_worker

    adapter = object()
    calls: list[tuple[int, object, float]] = []

    async def _process_pending(*, limit, adapter, approved_after):
        calls.append((limit, adapter, approved_after))
        return {"ok": True, "processed": 0}

    monkeypatch.setattr(approval_execution_worker, "create_approval_execution_adapter", lambda: adapter)
    monkeypatch.setattr(
        approval_execution_worker.approval_execution,
        "process_pending_executions",
        _process_pending,
    )

    worker = approval_execution_worker.ApprovalExecutionWorker(
        interval_seconds=1.0,
        limit=3,
        approved_after=123.0,
    )

    result = await worker.tick()

    assert result == {"ok": True, "processed": 0}
    assert calls == [(3, adapter, 123.0)]


def test_worker_defaults_approved_after_to_startup_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default cutoff should be fixed when the worker starts."""
    from runtime import approval_execution_worker

    monkeypatch.setattr(approval_execution_worker.time, "time", lambda: 456.0)

    async def _process_pending(*, limit, adapter, approved_after):
        del limit, adapter, approved_after
        return {"ok": True, "processed": 0}

    worker = approval_execution_worker.ApprovalExecutionWorker(
        interval_seconds=0.01,
        process_pending=_process_pending,
        adapter_factory=object,
    ).start()
    try:
        assert worker.approved_after == 456.0
    finally:
        assert worker.stop(timeout=1.0) is True


def test_worker_loop_continues_after_tick_exception() -> None:
    """A failed tick should be logged and followed by the next tick."""
    from runtime.approval_execution_worker import ApprovalExecutionWorker

    calls: list[object] = []
    second_tick_seen = threading.Event()

    async def _process_pending(*, limit, adapter, approved_after):
        del limit, approved_after
        calls.append(adapter)
        if len(calls) == 1:
            raise RuntimeError("temporary coordinator failure")
        second_tick_seen.set()
        return {"ok": True, "processed": 0}

    worker = ApprovalExecutionWorker(
        interval_seconds=0.01,
        process_pending=_process_pending,
        adapter_factory=object,
    ).start()
    try:
        assert second_tick_seen.wait(timeout=1.0)
    finally:
        assert worker.stop(timeout=1.0) is True

    assert len(calls) >= 2


def test_real_adapter_factory_uses_package_import_with_hermes_toolsets_on_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real factory should not fall back to top-level audit_log imports."""
    project_root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(project_root / "hermes-agent"))

    import runtime

    if hasattr(runtime, "approval_execution_worker"):
        monkeypatch.setattr(runtime, "approval_execution_worker", runtime.approval_execution_worker)

    for module_name in (
        "runtime.approval_execution_worker",
        "aiops_toolsets_approval_execution",
        "aiops_toolsets_remediation_execution",
        "toolsets",
        "toolsets.approval_execution",
        "toolsets.approval_async",
        "toolsets.remediation_execution",
        "toolsets.audit_log",
        "toolsets.incident_store",
        "toolsets.k8s_write",
        "toolsets.operation_lock",
        "toolsets.remediation_health",
        "audit_log",
    ):
        monkeypatch.delitem(sys.modules, module_name, raising=False)

    hermes_toolsets = types.ModuleType("toolsets")
    hermes_toolsets.__file__ = str(project_root / "hermes-agent" / "toolsets.py")
    monkeypatch.setitem(sys.modules, "toolsets", hermes_toolsets)

    from runtime.approval_execution_worker import create_approval_execution_adapter

    adapter = create_approval_execution_adapter()

    assert type(adapter).__name__ == "RemediationExecutionAdapter"
    assert type(adapter).__module__ == "toolsets.remediation_execution"
    assert sys.modules["toolsets"] is hermes_toolsets
    assert "audit_log" not in sys.modules
