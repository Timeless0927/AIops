from __future__ import annotations

import importlib
from pathlib import Path


def test_default_db_paths_follow_aiops_data_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))

    incident_store = importlib.reload(importlib.import_module("toolsets.incident_store"))
    message_delivery = importlib.reload(importlib.import_module("toolsets.message_delivery"))
    approval_async = importlib.reload(importlib.import_module("toolsets.approval_async"))
    system_mode = importlib.reload(importlib.import_module("toolsets.system_mode"))
    audit_log = importlib.reload(importlib.import_module("toolsets.audit_log"))
    operation_lock = importlib.reload(importlib.import_module("toolsets.operation_lock"))
    cost_guard = importlib.reload(importlib.import_module("toolsets.cost_guard"))
    rejection_learner = importlib.reload(importlib.import_module("toolsets.rejection_learner"))

    assert incident_store._default_db_path() == tmp_path / "incidents.db"
    assert message_delivery._default_db_path() == tmp_path / "message_deliveries.db"
    assert approval_async._default_db_path() == tmp_path / "approvals.db"
    assert system_mode._default_db_path() == tmp_path / "system_mode.db"
    assert audit_log._default_db_path() == tmp_path / "audit_log.db"
    assert operation_lock._default_db_path() == tmp_path / "operation_locks.db"
    assert cost_guard._default_db_path() == tmp_path / "cost_tracking.db"
    assert rejection_learner._lessons_path() == tmp_path / "rejection_lessons.json"
