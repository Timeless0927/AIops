"""Fixed MVP runbook skeleton catalog and enable-state store."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


JSON = dict[str, Any]
RUNBOOK_IDS = ("service_health", "k8s_workload", "dependency")
_MVP_SCOPE = {"cluster": "prod-a", "namespace": "default", "service": "checkout", "team": "payments"}
_MVP_OWNER = "sre-platform"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runbook_states (
    runbook_id TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL,
    updated_at REAL NOT NULL,
    updated_by TEXT NOT NULL
);
"""

_SKELETONS: tuple[JSON, ...] = (
    {
        "id": "service_health",
        "title": "Service health",
        "scope": dict(_MVP_SCOPE),
        "owner": _MVP_OWNER,
    },
    {
        "id": "k8s_workload",
        "title": "K8s workload",
        "scope": dict(_MVP_SCOPE),
        "owner": _MVP_OWNER,
    },
    {
        "id": "dependency",
        "title": "Dependency",
        "scope": dict(_MVP_SCOPE),
        "owner": _MVP_OWNER,
    },
)


class RunbookServiceError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_db_path() -> Path:
    env_dir = os.getenv("AIOPS_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser() / "runbooks.db"
    return _project_root() / "data" / "runbooks.db"


class RunbookDB:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or _default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=1.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA_SQL)
        self._seed()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _seed(self) -> None:
        now = time.time()
        with self._lock:
            for runbook_id in RUNBOOK_IDS:
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO runbook_states (runbook_id, enabled, updated_at, updated_by)
                    VALUES (?, 1, ?, 'system')
                    """,
                    (runbook_id, now),
                )

    def list_states(self) -> dict[str, JSON]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM runbook_states").fetchall()
        return {
            str(row["runbook_id"]): {
                "enabled": bool(int(row["enabled"])),
                "updated_at": float(row["updated_at"]),
                "updated_by": str(row["updated_by"]),
            }
            for row in rows
        }

    def set_enabled(self, runbook_id: str, enabled: bool, *, actor_id: str) -> JSON:
        if runbook_id not in RUNBOOK_IDS:
            raise RunbookServiceError("not_found", "runbook skeleton not found")
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO runbook_states (runbook_id, enabled, updated_at, updated_by)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(runbook_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at,
                    updated_by = excluded.updated_by
                """,
                (runbook_id, 1 if enabled else 0, now, actor_id),
            )
        return self.list_states()[runbook_id]


_DB = RunbookDB()


def list_runbooks(*, runs: list[JSON] | None = None) -> list[JSON]:
    states = _DB.list_states()
    return [
        {
            **skeleton,
            **states[skeleton["id"]],
            "last_run_summary": _last_run_summary(str(skeleton["id"]), runs or []),
        }
        for skeleton in _SKELETONS
    ]


def set_enabled(runbook_id: str, enabled: bool, *, actor_id: str, runs: list[JSON] | None = None) -> JSON:
    _DB.set_enabled(runbook_id, enabled, actor_id=actor_id)
    return next(item for item in list_runbooks(runs=runs) if item["id"] == runbook_id)


def _last_run_summary(runbook_id: str, runs: list[JSON]) -> JSON:
    matches = [run for run in runs if run.get("runbook_skeleton") == runbook_id]
    if not matches:
        return {"run_id": None, "status": "never_run", "summary": "No runs recorded", "updated_at": None}
    run = max(matches, key=lambda item: float(item.get("updated_at") or item.get("created_at") or 0))
    metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
    return {
        "run_id": run.get("run_id"),
        "status": run.get("status") or metadata.get("status") or "unknown",
        "summary": run.get("title") or "Agent run",
        "updated_at": run.get("updated_at") or run.get("created_at"),
    }
